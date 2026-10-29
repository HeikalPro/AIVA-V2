"""The scheduler (due-source claiming, the optimistic-claim race, periodic health checks) and the
Phase 2 runtime wiring (V002 install check, scheduler only when enabled, stop order)."""
from __future__ import annotations

import asyncio
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.doc_intel import crm_sync, schedule
from backend.doc_intel import runtime as runtime_module
from backend.doc_intel.constants import REQUIRED_TABLES_V001, REQUIRED_TABLES_V002
from backend.doc_intel.health import run_checks
from backend.doc_intel.kb_repo import parse_utc
from backend.doc_intel.runtime import CRM_NOT_INSTALLED_DETAIL, stop_doc_intel
from backend.doc_intel.scheduler import DocIntelScheduler
from backend.doc_intel.textutil import utc_now

from ._crm_fakes import CrmEnv, crm_env  # noqa: F401  (crm_env is a fixture)
from .conftest import Env, RecordingDatabase, make_runtime


def scheduler_for(crm: CrmEnv, now, **kw) -> DocIntelScheduler:
    return DocIntelScheduler(settings=crm.settings, crm_repo=crm.repo, clock=lambda: now, **kw)


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


# ---- due syncs ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_due_source_is_claimed_and_gets_a_scheduled_run(crm_env: CrmEnv):
    now = utc_now().replace(microsecond=0)
    due = now - timedelta(minutes=1)
    source_id = crm_env.seed_source(sync_enabled=1, sync_interval_days=7, sync_hour=2, next_sync_at=due)
    untouched = {
        crm_env.seed_source(sync_enabled=1, next_sync_at=now + timedelta(hours=1)): now + timedelta(hours=1),  # not due
        crm_env.seed_source(sync_enabled=0, next_sync_at=due): due,  # schedule off
        crm_env.seed_source(sync_enabled=1, next_sync_at=due, status="DISABLED"): due,
        crm_env.seed_source(sync_enabled=1, next_sync_at=due, status="DELETED"): due,
        crm_env.seed_source(sync_enabled=1, next_sync_at=None): None,
    }
    wakes: list[int] = []
    scheduler = scheduler_for(crm_env, now, wake_sync=lambda: wakes.append(1))
    summary = await scheduler.tick()
    assert summary == {"queued": [source_id], "lost": [], "already_active": [], "failed": [], "health": False}
    assert wakes == [1]
    (run,) = crm_env.repo.runs.values()
    assert (run["source_id"], run["trigger_type"], run["triggered_by"], run["status"]) == (source_id, "SCHEDULED", None, "QUEUED")
    expected = schedule.next_after_run(now, interval_days=7, hour=2, tz_name=crm_env.settings.timezone)
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == expected and expected > now
    for other, value in untouched.items():
        assert parse_utc(crm_env.source(other)["next_sync_at"]) == value
    again = await scheduler.tick()  # the claim moved next_sync_at: nothing is due any more
    assert again["queued"] == [] and len(crm_env.repo.runs) == 1 and wakes == [1]


@pytest.mark.asyncio
async def test_two_schedulers_racing_for_the_same_due_time_queue_one_run(crm_env: CrmEnv):
    now = utc_now().replace(microsecond=0)
    source_id = crm_env.seed_source(sync_enabled=1, next_sync_at=now - timedelta(minutes=5))
    seen_before_any_claim = await crm_env.repo.due_sources(now)

    class StaleView:
        """A second process's view: it read the due row before the first process claimed it."""

        def __getattr__(self, name):
            return getattr(crm_env.repo, name)

        async def due_sources(self, now, limit=100):
            return copy.deepcopy(seen_before_any_claim)

    first = scheduler_for(crm_env, now)
    second = DocIntelScheduler(settings=crm_env.settings, crm_repo=StaleView(), clock=lambda: now)
    one, two = await asyncio.gather(first.tick(), second.tick())
    assert sorted([one["queued"], two["queued"]]) == [[], [source_id]]
    assert sorted([one["lost"], two["lost"]]) == [[], [source_id]]
    assert len(crm_env.repo.runs) == 1


@pytest.mark.asyncio
async def test_a_due_time_with_fractional_seconds_is_claimed_exactly_once(crm_env: CrmEnv):
    """F24: a next_sync_at holding fractional seconds (written by SQL, e.g. SYSTIMESTAMP, or by
    hand) was never claimed again: the claim compared it for equality with a DATE bind, which
    drops the fractions. The fake repo binds like python-oracledb (fractions dropped)."""
    now = utc_now().replace(microsecond=0)
    due = (now - timedelta(minutes=1)).replace(microsecond=654321)
    source_id = crm_env.seed_source(sync_enabled=1, sync_interval_days=7, sync_hour=2, next_sync_at=due)
    seen_before_any_claim = await crm_env.repo.due_sources(now)
    assert [int(r["id"]) for r in seen_before_any_claim] == [source_id]
    assert parse_utc(seen_before_any_claim[0]["next_sync_at"]).microsecond == 654321

    class StaleView:
        """A second process's view: it read the due row before the first process claimed it."""

        def __getattr__(self, name):
            return getattr(crm_env.repo, name)

        async def due_sources(self, now, limit=100):
            return copy.deepcopy(seen_before_any_claim)

    first = scheduler_for(crm_env, now)
    second = DocIntelScheduler(settings=crm_env.settings, crm_repo=StaleView(), clock=lambda: now)
    one, two = await asyncio.gather(first.tick(), second.tick())
    assert sorted([one["queued"], two["queued"]]) == [[], [source_id]]
    assert sorted([one["lost"], two["lost"]]) == [[], [source_id]]
    (run,) = crm_env.repo.runs.values()
    assert (run["source_id"], run["trigger_type"]) == (source_id, "SCHEDULED")
    expected = schedule.next_after_run(now, interval_days=7, hour=2, tz_name=crm_env.settings.timezone)
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == expected
    # Later ticks find nothing due (the claim moved it), and a source due again is claimed again.
    assert (await first.tick())["queued"] == [] and len(crm_env.repo.runs) == 1
    crm_env.repo.runs[run["id"]]["status"] = "COMPLETED"
    crm_env.source(source_id)["next_sync_at"] = (now - timedelta(seconds=5)).replace(microsecond=1).isoformat()
    assert (await first.tick())["queued"] == [source_id] and len(crm_env.repo.runs) == 2


@pytest.mark.asyncio
async def test_an_undone_claim_is_restored_even_when_the_due_time_had_fractions(crm_env: CrmEnv):
    now = utc_now().replace(microsecond=0)
    due = (now - timedelta(minutes=3)).replace(microsecond=999999)
    source_id = crm_env.seed_source(sync_enabled=1, next_sync_at=due)
    crm_env.repo.raise_on["create_run"] = RuntimeError("DPY-4011: the database closed the connection")
    scheduler = scheduler_for(crm_env, now)
    assert (await scheduler.tick())["failed"] == [source_id] and crm_env.repo.runs == {}
    restored = parse_utc(crm_env.source(source_id)["next_sync_at"])
    assert restored == due.replace(microsecond=0) and restored <= now  # still due (the bind dropped the fraction)
    assert (await scheduler.tick())["queued"] == [source_id]


@pytest.mark.asyncio
async def test_an_active_run_covers_the_schedule(crm_env: CrmEnv):
    now = utc_now().replace(microsecond=0)
    source_id = crm_env.seed_source(sync_enabled=1, next_sync_at=now - timedelta(minutes=1))
    manual = await crm_env.repo.create_run(source_id, trigger_type="MANUAL", triggered_by=101)
    wakes: list[int] = []
    summary = await scheduler_for(crm_env, now, wake_sync=lambda: wakes.append(1)).tick()
    assert summary["already_active"] == [source_id] and summary["queued"] == [] and wakes == []
    assert list(crm_env.repo.runs) == [manual]
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) > now  # claimed: not re-fired every tick


@pytest.mark.asyncio
async def test_a_run_that_cannot_be_queued_is_retried_on_the_next_tick(crm_env: CrmEnv):
    now = utc_now().replace(microsecond=0)
    due = now - timedelta(minutes=1)
    source_id = crm_env.seed_source(sync_enabled=1, next_sync_at=due)
    crm_env.repo.raise_on["create_run"] = RuntimeError("DPY-4011: the database closed the connection")
    scheduler = scheduler_for(crm_env, now)
    summary = await scheduler.tick()
    assert summary["failed"] == [source_id] and crm_env.repo.runs == {}
    assert parse_utc(crm_env.source(source_id)["next_sync_at"]) == due  # the claim was undone
    assert (await scheduler.tick())["queued"] == [source_id]


@pytest.mark.asyncio
async def test_a_tick_never_raises(crm_env: CrmEnv, env: Env, caplog):
    crm_env.repo.raise_on["due_sources"] = RuntimeError("ORA-03113: end-of-file on communication channel")
    env.health_repo.load_error = RuntimeError("ORA-03113")
    scheduler = DocIntelScheduler(settings=crm_env.settings, crm_repo=crm_env.repo, health_deps=env.health_deps())
    summary = await scheduler.tick()
    assert summary["queued"] == [] and summary["health"] is False
    assert "could not read the due SharePoint syncs" in caplog.text and "scheduled health checks failed" in caplog.text


# ---- health cadence ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_checks_run_when_the_newest_result_is_older_than_the_interval(env: Env):
    ran: list[object] = []

    async def runner(deps):
        ran.append(deps)

    deps = env.health_deps()
    scheduler = DocIntelScheduler(settings=env.settings, health_deps=deps, health_runner=runner)
    assert (await scheduler.tick())["health"] is True  # nothing stored yet
    fresh = utc_now().isoformat()
    env.health_repo.rows = {"database": {"component_key": "database", "checked_at": fresh},
                            "crm": {"component_key": "crm", "checked_at": (utc_now() - timedelta(days=1)).isoformat()}}
    assert (await scheduler.tick())["health"] is False  # the newest result is fresh
    stale = (utc_now() - timedelta(minutes=env.settings.health_interval_minutes + 1)).isoformat()
    env.health_repo.rows["database"]["checked_at"] = stale
    assert (await scheduler.tick())["health"] is True
    assert len(ran) == 2 and ran[0] is deps


@pytest.mark.asyncio
async def test_scheduled_health_checks_store_their_results(env: Env):
    deps = env.health_deps()
    scheduler = DocIntelScheduler(settings=env.settings, health_deps=deps)
    assert (await scheduler.tick())["health"] is True
    assert set(env.health_repo.rows) == {"microsoft_graph", "crm", "knowledge_sync", "extraction", "embedding", "database"}
    assert (await scheduler.tick())["health"] is False


@pytest.mark.asyncio
async def test_the_ticker_runs_until_stopped(crm_env: CrmEnv):
    ticks: list[int] = []

    class Counting(DocIntelScheduler):
        async def tick(self):
            ticks.append(1)
            return await super().tick()

    scheduler = Counting(settings=crm_env.settings, crm_repo=crm_env.repo, tick_seconds=0.05, initial_delay_seconds=0)
    scheduler.start()
    scheduler.start()  # idempotent
    try:
        assert scheduler.running
        await wait_for(lambda: len(ticks) >= 3)
    finally:
        await scheduler.stop()
    assert not scheduler.running
    count = len(ticks)
    await asyncio.sleep(0.15)
    assert len(ticks) == count
    await scheduler.stop()  # stopping twice is fine


# ---- runtime wiring ---------------------------------------------------------------------------------


def install_check_db(*, v002: bool = True) -> RecordingDatabase:
    db = RecordingDatabase()
    tables = list(REQUIRED_TABLES_V001) + (list(REQUIRED_TABLES_V002) if v002 else [])
    db.on(r"FROM user_tables", [{"table_name": t.upper()} for t in tables])
    db.on(r"FROM AIVA_di_schema_version", lambda sql, params: {"version": params["version"]})
    return db


async def start_phase2(env: Env, db: RecordingDatabase, monkeypatch, *, scheduler: bool):
    monkeypatch.setattr(crm_sync, "sweep_stale_temp_dirs", lambda *a, **k: 0)  # never touch the real temp dir
    env.settings.scheduler_enabled = scheduler
    runtime = make_runtime(env, worker=None)
    await runtime_module._start_phase2(runtime, db)
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_phase2_starts_when_v002_is_installed_and_the_scheduler_only_when_enabled(env: Env, monkeypatch, enabled):
    runtime = await start_phase2(env, install_check_db(), monkeypatch, scheduler=enabled)
    try:
        assert runtime.crm_installed is True and runtime.crm_detail is None
        assert runtime.sync_service is not None and runtime.sync_worker_running
        assert runtime.health.crm_repo is runtime.sync_service.repo and runtime.health.sync_worker_running() is True
        assert runtime.health.secret_box_factory is not None and runtime.health.graph_factory is not None
        if enabled:
            assert runtime.scheduler is not None and runtime.scheduler_running
            assert runtime.scheduler._repo is runtime.sync_service.repo
        else:
            assert runtime.scheduler is None and not runtime.scheduler_running
    finally:
        await stop_doc_intel(runtime)
    assert not runtime.sync_worker_running and not runtime.scheduler_running


@pytest.mark.asyncio
async def test_without_v002_phase1_keeps_running_and_the_scheduler_only_checks_health(env: Env, monkeypatch):
    runtime = await start_phase2(env, install_check_db(v002=False), monkeypatch, scheduler=True)
    try:
        assert runtime.crm_installed is False and runtime.crm_detail.startswith("Missing tables: AIVA_CRM_SOURCES")
        assert runtime.sync_service is None and runtime.sync_worker is None and runtime.health.crm_repo is None
        assert runtime.scheduler is not None and runtime.scheduler._repo is None  # health checks only
        assert runtime.service is not None  # Phase 1 untouched
    finally:
        await stop_doc_intel(runtime)
    assert CRM_NOT_INSTALLED_DETAIL.endswith("run migration V002")


@pytest.mark.asyncio
async def test_a_phase2_start_failure_leaves_phase1_running(env: Env, monkeypatch):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("worker could not be built")

    monkeypatch.setattr(crm_sync, "SyncWorker", Broken)
    runtime = await start_phase2(env, install_check_db(), monkeypatch, scheduler=False)
    assert runtime.crm_installed is True and runtime.sync_service is None and runtime.sync_worker is None
    assert runtime.crm_detail == "SharePoint sync failed to start: RuntimeError: worker could not be built"
    assert runtime.health.crm_repo is None and runtime.health.graph_factory is None
    assert runtime.service is not None and runtime.health is not None


@pytest.mark.asyncio
async def test_a_failing_install_check_disables_only_phase2(env: Env, monkeypatch):
    db = RecordingDatabase()

    async def broken(sql, params=None, **_):
        raise RuntimeError("DPY-6005: cannot connect")

    db.fetch_all = broken
    runtime = await start_phase2(env, db, monkeypatch, scheduler=False)
    assert runtime.crm_installed is False and runtime.crm_detail == "Could not check the SharePoint sync tables: RuntimeError"


@pytest.mark.asyncio
async def test_start_and_stop_doc_intel_with_both_migrations_installed(env: Env, monkeypatch):
    from fastapi import FastAPI

    monkeypatch.setattr(runtime_module, "get_doc_intel_settings", lambda: env.settings)  # never the real .env
    monkeypatch.setattr(crm_sync, "sweep_stale_temp_dirs", lambda *a, **k: 0)
    env.settings.scheduler_enabled = True
    app = FastAPI()
    embedding_svc = SimpleNamespace(
        settings=SimpleNamespace(oracle_dsn="db.example:1521/FREEPDB1"), db=SimpleNamespace(connection=lambda: None)
    )
    runtime = await runtime_module.start_doc_intel(app, install_check_db(), embedding_svc)
    try:
        assert app.state.doc_intel is runtime
        assert (runtime.installed, runtime.schema_version, runtime.crm_installed) == (True, "001", True)
        assert runtime.worker_running and runtime.sync_worker_running and runtime.scheduler_running
        assert runtime.health.crm_repo is runtime.sync_service.repo
    finally:
        await stop_doc_intel(runtime)
    assert not (runtime.worker_running or runtime.sync_worker_running or runtime.scheduler_running)


@pytest.mark.asyncio
async def test_stop_order_and_it_never_raises():
    order: list[str] = []

    class Component:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name, self.fail, self.running = name, fail, True

        def request_stop(self) -> None:
            order.append(f"{self.name}.request_stop")

        async def stop(self) -> None:
            order.append(f"{self.name}.stop")
            if self.fail:
                raise RuntimeError("stop failed")

    runtime = runtime_module.DocIntelRuntime(settings=SimpleNamespace())
    runtime.worker = Component("import")
    runtime.sync_worker = Component("sync", fail=True)
    runtime.scheduler = Component("scheduler")
    runtime.extraction = SimpleNamespace(terminate_active_extractions=lambda: order.append("kill") or 0)
    await stop_doc_intel(runtime)  # the sync worker's error is logged, the rest still stops
    assert order == ["sync.request_stop", "kill", "scheduler.stop", "sync.stop", "import.stop", "kill"]
    await stop_doc_intel(None)
    await stop_doc_intel(runtime_module.DocIntelRuntime(settings=SimpleNamespace()))


@pytest.mark.asyncio
async def test_run_checks_is_the_default_health_runner(env: Env):
    scheduler = DocIntelScheduler(settings=env.settings, health_deps=env.health_deps())
    assert scheduler._health_runner is run_checks
