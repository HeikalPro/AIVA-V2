"""Opt-in integration tests of CrmRepo against the configured Oracle schema (``DOC_INTEL_IT=1``).

The schema is shared with live production, so these tests follow the isolation rules of
test_integration_oracle.py, with the same savepoint wrapper (``TxDatabase``):

* Nothing is ever committed. Each test runs on ONE async connection; every transaction the
  code under test opens becomes a SAVEPOINT (rolled back to on error, as the real
  ``Database.connection`` rolls back), and the session is rolled back when the test ends,
  pass or fail. A crashed run's session is rolled back by the database. No DDL runs.
* Rows are written only to AIVA_CRM_SOURCES, AIVA_CRM_SYNC_RUNS, AIVA_CRM_SOURCE_FILES and
  AIVA_CRM_ENTITIES, and carry this run's markers: source names ``di-it-crm-<run>-...``,
  ``created_by`` / ``updated_by`` / ``triggered_by`` in 990000100-990000999, item ids and match
  keys containing ``di-it-<run>``. AIVA_accounts / AIVA_users are only read (LEFT JOINs).
* ``claim_next_run`` (the oldest QUEUED run of the table) and ``recover_stale_runs`` (every
  stale RUNNING run) run only after a read-only guard shows they can reach this run's rows only.
* No Microsoft call and no extraction: this is the SQL of the CRM store only.

Run from AIVA-V2 (PowerShell: ``$env:DOC_INTEL_IT = "1"`` first):

    DOC_INTEL_IT=1 python -m pytest backend/tests/doc_intel/test_integration_oracle_crm.py -q -p no:cacheprovider -rs

Without ``DOC_INTEL_IT=1`` every test is skipped; an unreachable database or missing V002
tables skip the module with the reason. The last test re-counts this run's markers from a
fresh session and must find zero rows.
"""
from __future__ import annotations

import json
import os
import random
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import oracledb
import pytest
import pytest_asyncio

from backend.config import Settings
from backend.doc_intel.crm_repo import (
    INTERRUPTED_FILE_REASON,
    INTERRUPTED_RUN_REASON,
    CrmRepo,
    EntityRow,
    RunAlreadyActive,
    build_entity_rows,
    entity_to_out,
    file_to_out,
    source_to_out,
)
from backend.doc_intel.kb_repo import loads_json, parse_utc
from backend.doc_intel.textutil import utc_now

from ._crm_fakes import FakeBox, remote
from .test_integration_oracle import CALL_TIMEOUT_MS, TxDatabase, _connect_kwargs, _error_code

pytestmark = pytest.mark.skipif(
    os.environ.get("DOC_INTEL_IT") != "1",
    reason="opt-in: set DOC_INTEL_IT=1 to run the Oracle integration tests",
)

RUN = uuid.uuid4().hex[:10]
MARKER = 990000100 + random.SystemRandom().randrange(900)  # created_by / triggered_by
NAME_PREFIX = f"di-it-crm-{RUN}"
ITEM_PREFIX = f"di-it-{RUN}"
SOURCE_IDS: list[int] = []  # every source id this run created (for the final count)
TABLES = ("AIVA_CRM_SOURCES", "AIVA_CRM_SYNC_RUNS", "AIVA_CRM_SOURCE_FILES", "AIVA_CRM_ENTITIES")
ANCIENT = datetime(2000, 1, 1)
ARABIC = "عقد توريد مع شركة النيل للتجارة والتوزيع "
WORKER = f"di-it-worker-{RUN}"


@pytest.fixture(scope="session")
def oracle_crm() -> dict[str, Any]:
    """Connection settings of the configured schema; skips the module when V002 is unusable."""
    kwargs = _connect_kwargs(Settings())
    try:
        conn = oracledb.connect(**kwargs)
    except oracledb.Error as ex:
        pytest.skip(f"DOC_INTEL_IT=1 but the database is unreachable ({_error_code(ex)})")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM user_tables WHERE table_name IN (:t0, :t1, :t2, :t3)",
                {f"t{i}": name for i, name in enumerate(TABLES)},
            )
            found = {r[0] for r in cur.fetchall()}
            ledger = None
            if "AIVA_CRM_SOURCES" in found:
                cur.execute("SELECT version FROM AIVA_di_schema_version WHERE version = '002'")
                ledger = cur.fetchone()
    finally:
        conn.close()
    missing = sorted(set(TABLES) - found)
    if missing:
        pytest.skip(f"migration V002 tables missing in the connected schema: {', '.join(missing)}")
    if ledger is None:
        pytest.skip("migration V002 is not recorded in AIVA_di_schema_version")
    return kwargs


@pytest_asyncio.fixture
async def db(oracle_crm: dict[str, Any]) -> AsyncIterator[TxDatabase]:
    conn = await oracledb.connect_async(**oracle_crm)
    conn.call_timeout = CALL_TIMEOUT_MS
    try:
        yield TxDatabase(conn)
    finally:
        try:
            await conn.rollback()
        finally:
            await conn.close()


# ---- helpers -------------------------------------------------------------------------------------------


async def new_source(repo: CrmRepo, box: FakeBox, suffix: str = "src", **kw: Any) -> int:
    values: dict[str, Any] = {
        "name": f"{NAME_PREFIX}-{suffix}",
        "account_id": MARKER,
        "tenant_id_enc": box.encrypt("8f14e45f-ceea-467a-9575-0c4a1b2d3e4f"),
        "client_id_enc": box.encrypt("c9f0f895-fb98-4b91-a1c2-3d4e5f60718a"),
        "client_secret_enc": box.encrypt("di-it-not-a-real-secret-0000"),
        "client_secret_hint": "…0000",
        "secret_updated_at": utc_now(),
        "site_url": "https://contoso.sharepoint.com/sites/DiIt",
        "drive_name": None,
        "folder_path": "/CRM",
        "recursive": True,
        "file_extensions": [".pdf", ".docx"],
        "sync_enabled": False,
        "sync_interval_days": 14,
        "sync_hour": 2,
        "next_sync_at": None,
        "created_by": MARKER,
    }
    values.update(kw)
    source_id = await repo.insert_source(**values)
    SOURCE_IDS.append(source_id)
    return source_id


def item(name: str) -> str:
    return f"{ITEM_PREFIX}-{name}"


async def scalar(db: TxDatabase, sql: str, params: dict[str, Any] | None = None) -> Any:
    row = await db.fetch_one(sql, params)
    return None if row is None else next(iter(row.values()))


async def force_running(db: TxDatabase, run_id: int, *, worker: str = WORKER, updated_at: datetime | None = None) -> None:
    """What a claim does, aimed at this run's row only (claim_next_run takes the table's oldest)."""
    now = utc_now()
    await db.execute(
        "UPDATE AIVA_crm_sync_runs SET status = 'RUNNING', worker_id = :w, started_at = :now, updated_at = :u "
        "WHERE id = :id AND triggered_by = :m",
        {"w": worker, "now": now, "u": updated_at or now, "id": run_id, "m": MARKER},
    )


def entities(n: int, tag: str) -> list[EntityRow]:
    raw = [
        {"full_name": f"{ARABIC}{i}", "email": f"{ITEM_PREFIX}-{tag}-{i}@Example.com",
         "_meta": {"entity_type": "contact", "confidence": 0.8, "fields": {"full_name": {"provenance": [{"page": 1, "source_text": ARABIC}]}}}}
        for i in range(n)
    ]
    return build_entity_rows(raw)


# ---- sources ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_round_trip_update_resolve_and_soft_delete(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box, name=f"{NAME_PREFIX}-{ARABIC * 20}")  # > 512 bytes of Arabic
    row = await repo.get_source(source_id)
    assert row["status"] == "ACTIVE" and row["client_secret_set"] == 1 and "client_secret_enc" not in row
    assert len(row["name"].encode("utf-8")) <= 512 and row["name"].startswith(NAME_PREFIX)
    assert row["account_name"] is None and row["file_extensions"] == ".pdf,.docx" and row["recursive"] == 1
    out = source_to_out(row, box=box)
    assert out.tenant_id == "8f14e45f-ceea-467a-9575-0c4a1b2d3e4f" and out.created_at.endswith("Z")
    assert abs((parse_utc(out.created_at) - utc_now()).total_seconds()) < 600  # stored as UTC
    assert source_id in {int(r["id"]) for r in await repo.list_sources()}
    creds = await repo.get_credentials(source_id)
    assert box.decrypt(creds["client_secret_enc"]) == "di-it-not-a-real-secret-0000"

    assert await repo.update_source(source_id, {"folder_path": "/CRM/" + ARABIC, "sync_hour": 5}, updated_by=MARKER)
    target = type("T", (), {"site_id": "site-x", "drive_id": "drive-x", "folder_id": "folder-x"})()
    # DECODE matches the NULL drive_name; a stale location does not match.
    assert await repo.set_resolved(source_id, target, site_url="https://contoso.sharepoint.com/sites/DiIt",
                                   drive_name=None, folder_path="/CRM/" + ARABIC)
    assert not await repo.set_resolved(source_id, target, site_url="https://contoso.sharepoint.com/sites/DiIt",
                                       drive_name=None, folder_path="/CRM")
    row = await repo.get_source(source_id)
    assert (row["resolved_folder_id"], row["sync_hour"], row["updated_by"]) == ("folder-x", 5, MARKER)

    queued = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    file_id = await repo.upsert_file(source_id, remote(item("del")), run_id=queued)
    await repo.replace_file_entities(file_id, entities(2, "del"), source_id=source_id, account_id=MARKER)
    with pytest.raises(RunAlreadyActive):  # refused while a sync is queued (or running)
        await repo.soft_delete_source(source_id, updated_by=MARKER)
    assert (await repo.get_source(source_id))["status"] == "ACTIVE"  # the refusal changed nothing
    await db.execute(
        "UPDATE AIVA_crm_sync_runs SET status = 'COMPLETED' WHERE id = :id AND triggered_by = :m", {"id": queued, "m": MARKER}
    )
    assert await repo.soft_delete_source(source_id, updated_by=MARKER) == 2  # entities withdrawn
    assert await repo.get_source(source_id) is None and await repo.get_credentials(source_id) is None
    deleted = await repo.get_source(source_id, include_deleted=True)
    assert (deleted["status"], deleted["client_secret_set"], deleted["next_sync_at"]) == ("DELETED", 0, None)
    rows, total = await repo.list_entities(source_id=source_id)
    assert total == 2 and {r["status"] for r in rows} == {"WITHDRAWN"} and all(r["withdrawn_at"] for r in rows)
    assert rows[0]["source_status"] == "DELETED" and entity_to_out(rows[0]).source_name == rows[0]["source_name"] + " (deleted)"
    assert (await repo.get_run(queued))["status"] == "COMPLETED" and (await repo.get_file(file_id))["state"] == "ACTIVE"
    assert await repo.soft_delete_source(source_id, updated_by=MARKER) is None
    assert await repo.update_source(source_id, {"name": "x"}, updated_by=MARKER) is False


@pytest.mark.asyncio
async def test_check_constraints_reject_bad_values(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo.update_source(source_id, {"status": "BOGUS"}, updated_by=MARKER)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo.update_source(source_id, {"sync_hour": 24}, updated_by=MARKER)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo.update_source(source_id, {"recursive": 2}, updated_by=MARKER)
    run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await db.execute("UPDATE AIVA_crm_sync_runs SET status = 'DONE' WHERE id = :id", {"id": run_id})
    other = await new_source(repo, box, "other")  # no active run: only the CHECK can refuse this one
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo.create_run(other, trigger_type="WEEKLY", triggered_by=MARKER)
    file_id = await repo.upsert_file(source_id, remote(item("chk")), run_id=run_id)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await db.execute("UPDATE AIVA_crm_source_files SET persist_status = 'DONE' WHERE id = :id", {"id": file_id})
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await db.execute("UPDATE AIVA_crm_source_files SET state = 'GONE' WHERE id = :id", {"id": file_id})
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await db.execute(
            "INSERT INTO AIVA_crm_entities (source_id, source_file_id, entity_type, status) VALUES (:s, :f, 'contact', 'GONE')",
            {"s": source_id, "f": file_id},
        )
    # Only the failed statements were undone; the session and the good rows are intact.
    assert (await repo.get_source(source_id))["status"] == "ACTIVE"
    assert (await repo.get_run(run_id))["status"] == "QUEUED"


# ---- runs ----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_unique_index_allows_one_queued_or_running_run_per_source(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    other = await new_source(repo, box, "other")
    first = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    with pytest.raises(RunAlreadyActive):  # ORA-00001 on UQ_AIVA_CRM_RUN_ACTIVE, mapped
        await repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=None)
    second_source_run = await repo.create_run(other, trigger_type="SCHEDULED", triggered_by=MARKER)
    assert second_source_run != first  # the index is per source

    await db.execute("UPDATE AIVA_crm_sync_runs SET created_at = :t WHERE id = :id", {"t": ANCIENT, "id": first})
    oldest = await scalar(db, "SELECT id FROM AIVA_crm_sync_runs WHERE status = 'QUEUED' ORDER BY created_at, id FETCH FIRST 1 ROWS ONLY")
    if oldest == first:
        claimed = await repo.claim_next_run(WORKER)
        assert claimed["id"] == first and claimed["status"] == "RUNNING" and claimed["worker_id"] == WORKER
    else:
        await force_running(db, first)
    with pytest.raises(RunAlreadyActive):  # RUNNING counts as active too
        await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    assert (await repo.active_runs(source_id))[source_id]["id"] == first
    assert await repo.finish_run(
        first, worker_id=WORKER, status="COMPLETED", error_message=None, counts={"files_seen": 2}, details={"x": 1},
        source_id=source_id, next_sync_at=None, schedule_interval_days=None, schedule_hour=None,
    )
    again = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)  # the index entry is gone
    assert again not in (first, second_source_run)
    rows, total = await repo.list_runs(source_id)
    assert total == 2 and [int(r["id"]) for r in rows] == [again, first]
    assert loads_json(rows[1]["details_json"], {}) == {"x": 1} and rows[1]["files_seen"] == 2


@pytest.mark.asyncio
async def test_finish_run_records_the_result_and_respects_a_changed_schedule(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box, sync_enabled=True, sync_interval_days=7, sync_hour=3,
                                 next_sync_at=datetime(2026, 1, 1, 0, 0))
    next_at = datetime(2030, 5, 6, 1, 0)
    run_id = await repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=MARKER)
    await force_running(db, run_id)
    long_error = "فشل: " + ARABIC * 120  # > 4000 bytes of UTF-8
    assert await repo.finish_run(
        run_id, worker_id=WORKER, status="PARTIAL", error_message=long_error, counts={"files_failed": 1},
        details={"listing_complete": False}, source_id=source_id, next_sync_at=next_at, schedule_interval_days=7, schedule_hour=3,
    )
    source = await repo.get_source(source_id)
    assert parse_utc(source["next_sync_at"]) == next_at and source["last_sync_status"] == "PARTIAL"
    assert source["last_success_at"] is None and len(source["last_sync_error"].encode("utf-8")) <= 4000
    assert await scalar(db, "SELECT LENGTHB(error_message) AS b FROM AIVA_crm_sync_runs WHERE id = :id", {"id": run_id}) <= 4000
    # finishing again is refused: the run is no longer RUNNING
    assert not await repo.finish_run(
        run_id, worker_id=WORKER, status="FAILED", error_message="late", counts={}, details={}, source_id=source_id,
        next_sync_at=None, schedule_interval_days=None, schedule_hour=None,
    )

    second = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    await force_running(db, second)
    # computed for interval 14 while the source now says 7: the admin's schedule wins
    assert await repo.finish_run(
        second, worker_id=WORKER, status="COMPLETED", error_message=None, counts={}, details={}, source_id=source_id,
        next_sync_at=datetime(2031, 1, 1, 0, 0), schedule_interval_days=14, schedule_hour=3,
    )
    source = await repo.get_source(source_id)
    assert parse_utc(source["next_sync_at"]) == next_at and source["last_success_at"] is not None
    assert source["last_sync_status"] == "COMPLETED" and source["last_sync_error"] is None
    assert int((await repo.last_runs())[source_id]["id"]) == second


@pytest.mark.asyncio
async def test_recover_stale_and_release_interrupted_runs(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    stale = await repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=MARKER)
    await force_running(db, stale, worker="di-it-dead", updated_at=ANCIENT)
    file_id = await repo.upsert_file(source_id, remote(item("stale")), run_id=stale)
    assert await repo.begin_file(file_id, stale)
    assert await repo.set_file_stage(file_id, "download", "COMPLETED")
    assert await repo.set_file_stage(file_id, "extraction", "RUNNING")
    threshold = ANCIENT + timedelta(days=1)
    foreign = await scalar(
        db,
        "SELECT COUNT(*) AS n FROM AIVA_crm_sync_runs WHERE status = 'RUNNING' AND updated_at < :t AND id <> :id",
        {"t": threshold, "id": stale},
    )
    if foreign:
        pytest.skip("another stale RUNNING run exists; recover_stale_runs would reach it")
    assert await repo.recover_stale_runs(threshold) == [stale]
    run = await repo.get_run(stale)
    assert (run["status"], run["error_message"]) == ("FAILED", INTERRUPTED_RUN_REASON) and run["finished_at"]
    out = file_to_out(await repo.get_file(file_id))
    assert (out.status, out.failed_stage, out.error_message) == ("FAILED", "extraction", INTERRUPTED_FILE_REASON)
    assert [s.status for s in out.stages] == ["COMPLETED", "FAILED", "PENDING", "PENDING", "PENDING"]
    assert (await repo.get_source(source_id))["last_sync_status"] == "FAILED"
    assert await repo.recover_stale_runs(threshold) == []

    live = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    await force_running(db, live, worker="di-it-other")
    await repo.touch_run(live, "someone-else")
    assert await repo.release_interrupted_run(live, "someone-else") is False
    assert await repo.release_interrupted_run(live, "di-it-other") is True
    assert (await repo.get_run(live))["status"] == "FAILED"


# ---- files and entities ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_upsert_stages_retry_and_deletion(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    first = remote(item("a"), name=ARABIC * 30 + ".pdf", modified_at=datetime(2026, 9, 1, 10, 0, 0))
    file_id = await repo.upsert_file(source_id, first, run_id=run_id)
    row = await repo.get_file(file_id)
    assert (row["state"], row["status"], row["attempts"], row["last_run_id"]) == ("ACTIVE", "PENDING", 0, run_id)
    assert len(row["name"].encode("utf-8")) <= 1024 and parse_utc(row["modified_at"]) == datetime(2026, 9, 1, 10, 0)
    assert (await repo.active_files(source_id))[0]["item_id"] == item("a")

    assert await repo.begin_file(file_id, run_id)
    assert await repo.set_file_stage(file_id, "download", "RUNNING")
    assert await repo.set_file_stage(file_id, "download", "COMPLETED", metrics={"size_bytes": 12})
    error = "تعذر الاستخراج: " + ARABIC * 150
    assert await repo.fail_file(file_id, "intelligence", error, completed=("extraction",), metrics={"code": "schema_error"})
    row = await repo.get_file(file_id)
    assert (row["status"], row["failed_stage"], row["attempts"]) == ("FAILED", "intelligence", 1)
    assert await scalar(db, "SELECT LENGTHB(error_message) AS b FROM AIVA_crm_source_files WHERE id = :id", {"id": file_id}) <= 4000
    details = loads_json(row["stage_details"], {})
    assert details["download"]["metrics"] == {"size_bytes": 12} and details["intelligence"]["metrics"] == {"code": "schema_error"}
    assert set(details) == {"download", "extraction", "intelligence"}
    assert await repo.set_file_stage(file_id, "persist", "RUNNING") is False  # not PROCESSING: refused

    assert await repo.reset_file_for_retry(file_id)
    row = await repo.get_file(file_id)
    assert (row["status"], row["attempts"], row["failed_stage"], loads_json(row["stage_details"], {})) == ("PENDING", 0, None, {})
    again = await repo.upsert_file(source_id, remote(item("a"), ctag="c2"), run_id=run_id, reset_attempts=False)
    assert again == file_id and (await repo.get_file(file_id))["ctag"] == "c2"
    with pytest.raises(oracledb.DatabaseError, match="ORA-00001"):  # uq_aiva_crm_file_item
        await db.execute(
            "INSERT INTO AIVA_crm_source_files (source_id, drive_id, item_id) VALUES (:s, 'drive-1', :i)",
            {"s": source_id, "i": item("a")},
        )

    before = (await repo.get_file(file_id))["updated_at"]
    await repo.mark_seen([file_id])
    await repo.refresh_file_metadata(file_id, remote(item("a"), name="renamed.pdf", ctag="c2"))
    row = await repo.get_file(file_id)
    assert row["name"] == "renamed.pdf" and row["last_seen_at"] and row["updated_at"] == before  # not an "activity"
    assert await repo.mark_files_deleted([file_id]) == 1 and await repo.mark_files_deleted([file_id]) == 0
    row = await repo.get_file(file_id)
    assert row["state"] == "DELETED" and row["deleted_at"] and await repo.active_files(source_id) == []
    assert await repo.upsert_file(source_id, remote(item("a")), run_id=run_id) == file_id  # re-activated
    assert (await repo.get_file(file_id))["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_entity_replace_is_atomic_and_stores_large_json(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    file_id = await repo.upsert_file(source_id, remote(item("e")), run_id=run_id)
    assert await repo.begin_file(file_id, run_id)
    big_result = {"extraction": {"text": ARABIC * 800}}  # > 32 KB of UTF-8, bound as one string
    assert len(json.dumps(big_result, ensure_ascii=False).encode("utf-8")) > 32767
    assert await repo.complete_file(
        file_id, source_id=source_id, account_id=MARKER, entities=entities(2, "v1"), result=big_result,
        warnings=[{"code": "low_text", "message": ARABIC, "page": 1}], is_valid=True, content_sha256="a" * 64,
    )
    row = await repo.get_file(file_id)
    assert (row["status"], row["persist_status"], row["entity_count"], row["is_valid"]) == ("COMPLETED", "COMPLETED", 2, 1)
    stored = await scalar(db, "SELECT result_json FROM AIVA_crm_source_files WHERE id = :id", {"id": file_id})
    assert json.loads(stored) == big_result
    v1 = {int(r["id"]) for r in (await repo.list_entities(source_id=source_id, status="ACTIVE"))[0]}
    assert len(v1) == 2

    replaced = await repo.replace_file_entities(file_id, entities(3, "v2"), source_id=source_id, account_id=MARKER)
    assert replaced == {"withdrawn": 2, "inserted": 3}
    active, total = await repo.list_entities(source_id=source_id, status="ACTIVE")
    assert total == 3 and v1.isdisjoint(int(r["id"]) for r in active)
    withdrawn, _ = await repo.list_entities(source_id=source_id, status="WITHDRAWN")
    assert {int(r["id"]) for r in withdrawn} == v1 and all(r["withdrawn_at"] for r in withdrawn)

    # A failing insert (entity_type NOT NULL) rolls the whole replace back: the v2 rows stay ACTIVE.
    broken = [*entities(1, "v3"), EntityRow(entity_type=None, display_value="x", match_key=None, confidence=None, is_valid=None)]  # type: ignore[arg-type]
    with pytest.raises(oracledb.DatabaseError, match="ORA-01400"):
        await repo.replace_file_entities(file_id, broken, source_id=source_id, account_id=MARKER)
    after, total = await repo.list_entities(source_id=source_id, status="ACTIVE")
    assert total == 3 and {int(r["id"]) for r in after} == {int(r["id"]) for r in active}

    out = entity_to_out(after[0])
    assert out.entity_type == "contact" and "_meta" not in out.fields and out.provenance["fields"]["full_name"]["provenance"][0]["page"] == 1
    assert out.display_value.startswith(ARABIC.strip()) and out.fields["email"].startswith(ITEM_PREFIX)
    found, total = await repo.list_entities(q=f"{ITEM_PREFIX}-v2-1@", source_id=source_id)
    assert total == 1 and found[0]["match_key"] == f"email:{ITEM_PREFIX}-v2-1@example.com"
    assert (await repo.list_entities(q="100%_", source_id=source_id))[1] == 0  # LIKE wildcards are literal
    assert await repo.withdraw_file_entities(file_id) == 3
    assert (await repo.counts_by_source(source_id))[source_id] == {
        "files_active": 1, "files_failed": 0, "files_deleted": 0, "entities_active": 0,
    }


@pytest.mark.asyncio
async def test_a_refused_persist_writes_nothing(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    file_id = await repo.upsert_file(source_id, remote(item("r")), run_id=run_id)  # PENDING, not PROCESSING
    assert await repo.complete_file(
        file_id, source_id=source_id, account_id=None, entities=entities(2, "r"), result={}, warnings=[],
        is_valid=True, content_sha256=None,
    ) is False
    assert (await repo.list_entities(source_id=source_id))[1] == 0


# ---- scheduling and monitoring ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_schedule_claim_on_real_timestamps(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    due = datetime(2020, 1, 1, 0, 0)  # schedule.py only produces whole hours
    source_id = await new_source(repo, box, sync_enabled=True, next_sync_at=due)
    now = utc_now()
    mine = [r for r in await repo.due_sources(now, limit=1000) if int(r["id"]) == source_id]
    assert len(mine) == 1
    old = parse_utc(mine[0]["next_sync_at"])
    assert old == due
    new = now + timedelta(days=14)  # fractions included: the restore below must still match it
    assert await repo.claim_due_source(source_id, now=now, new=new) is True
    assert await repo.claim_due_source(source_id, now=now, new=new) is False  # a second process loses
    assert await repo.restore_next_sync(source_id, expected=new, value=old) is True
    assert parse_utc((await repo.get_source(source_id))["next_sync_at"]) == due

    # Probe: python-oracledb binds a datetime as DATE, so fractional seconds are dropped on the
    # way in (the TIMESTAMP(6) column then holds .000000).
    fine = datetime(2020, 2, 2, 3, 4, 5, 678901)
    await repo.update_source(source_id, {"next_sync_at": fine}, updated_by=MARKER)
    read_back = parse_utc((await repo.get_source(source_id))["next_sync_at"])
    assert read_back == fine.replace(microsecond=0)
    assert await repo.claim_due_source(source_id, now=now, new=new) is True


class _OnlyThisRun:
    """The real CrmRepo, with ``due_sources`` narrowed to this run's sources (it is a read of the
    whole table): a scheduler tick then claims and queues only rows this run created."""

    def __init__(self, repo: CrmRepo, source_ids: set[int], *, fail_create: bool = False) -> None:
        self._repo, self._ids, self._fail_create = repo, source_ids, fail_create
        self.stale_view: list[dict[str, Any]] | None = None  # a second process's earlier read

    def __getattr__(self, name: str) -> Any:
        return getattr(self._repo, name)

    async def due_sources(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        if self.stale_view is not None:
            return [dict(r) for r in self.stale_view]
        return [r for r in await self._repo.due_sources(now, limit=1000) if int(r["id"]) in self._ids]

    async def create_run(self, source_id: int, **kw: Any) -> int:
        if self._fail_create:
            raise RuntimeError("DPY-4011: the database closed the connection (simulated)")
        return await self._repo.create_run(source_id, **kw)


@pytest.mark.asyncio
async def test_a_due_time_with_fractional_seconds_is_claimed_exactly_once(db: TxDatabase):
    """F24: the scheduler never claimed a source whose next_sync_at held fractional seconds (the
    claim compared it for equality with a DATE bind), so it was never auto-synced again."""
    from backend.doc_intel import schedule
    from backend.doc_intel.scheduler import DocIntelScheduler
    from backend.doc_intel.settings import DocIntelSettings

    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box, "frac", sync_enabled=True, sync_interval_days=7, sync_hour=2)
    guarded = {"id": source_id, "m": MARKER}
    await db.execute(  # an SQL-side write, fractions included (as a manual fix or SYSTIMESTAMP would)
        "UPDATE AIVA_crm_sources SET next_sync_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) - INTERVAL '1' MINUTE "
        "WHERE id = :id AND created_by = :m",
        guarded,
    )
    stored = parse_utc((await repo.get_source(source_id))["next_sync_at"])
    if stored.microsecond == 0:  # SYSTIMESTAMP landed on a whole second: make the fraction certain
        await db.execute(
            "UPDATE AIVA_crm_sources SET next_sync_at = next_sync_at + INTERVAL '0.25' SECOND WHERE id = :id AND created_by = :m",
            guarded,
        )
        stored = parse_utc((await repo.get_source(source_id))["next_sync_at"])
    assert stored.microsecond != 0
    now = utc_now()
    view = _OnlyThisRun(repo, {source_id})
    (row,) = await view.due_sources(now)
    old = parse_utc(row["next_sync_at"])
    assert old == stored and old <= now

    # The cause, on real Oracle: equality with the value read back never matches.
    async with db.connection() as conn:
        cur = conn.cursor()
        await cur.execute(
            "UPDATE AIVA_crm_sources SET next_sync_at = :new WHERE id = :id AND created_by = :m AND next_sync_at = :old",
            {**guarded, "old": old, "new": now + timedelta(days=7)},
        )
        assert cur.rowcount == 0

    settings = DocIntelSettings(_env_file=None, timezone="Africa/Cairo")
    first = DocIntelScheduler(settings=settings, crm_repo=view, clock=lambda: now)
    summary = await first.tick()
    assert summary["queued"] == [source_id] and summary["lost"] == [] and summary["failed"] == []
    expected = schedule.next_after_run(now, interval_days=7, hour=2, tz_name="Africa/Cairo")
    assert parse_utc((await repo.get_source(source_id))["next_sync_at"]) == expected
    # A second process that read the row before the claim loses; a later tick finds nothing due.
    stale = _OnlyThisRun(repo, {source_id})
    stale.stale_view = [row]
    second = DocIntelScheduler(settings=settings, crm_repo=stale, clock=lambda: now)
    assert (await second.tick())["lost"] == [source_id]
    assert (await first.tick())["queued"] == []
    runs, total = await repo.list_runs(source_id)
    assert total == 1 and (runs[0]["trigger_type"], runs[0]["status"], runs[0]["triggered_by"]) == ("SCHEDULED", "QUEUED", None)


@pytest.mark.asyncio
async def test_an_undone_claim_restores_a_due_time_that_had_fractions(db: TxDatabase):
    from backend.doc_intel.scheduler import DocIntelScheduler
    from backend.doc_intel.settings import DocIntelSettings

    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box, "undo", sync_enabled=True)
    guarded = {"id": source_id, "m": MARKER}
    await db.execute(
        "UPDATE AIVA_crm_sources SET next_sync_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) - INTERVAL '2' MINUTE "
        "+ INTERVAL '0.5' SECOND WHERE id = :id AND created_by = :m",
        guarded,
    )
    now = utc_now()
    settings = DocIntelSettings(_env_file=None, timezone="Africa/Cairo")
    failing = _OnlyThisRun(repo, {source_id}, fail_create=True)
    summary = await DocIntelScheduler(settings=settings, crm_repo=failing, clock=lambda: now).tick()
    assert summary["failed"] == [source_id] and summary["queued"] == []
    restored = parse_utc((await repo.get_source(source_id))["next_sync_at"])
    assert restored <= now  # the claim was undone: still due (the DATE bind dropped the fraction)
    assert (await repo.list_runs(source_id))[1] == 0
    working = _OnlyThisRun(repo, {source_id})
    assert (await DocIntelScheduler(settings=settings, crm_repo=working, clock=lambda: now).tick())["queued"] == [source_id]


@pytest.mark.asyncio
async def test_monitoring_queries_find_this_runs_rows(db: TxDatabase):
    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box)
    run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    await force_running(db, run_id, updated_at=ANCIENT)
    ok_file = await repo.upsert_file(source_id, remote(item("ok")), run_id=run_id)
    bad_file = await repo.upsert_file(source_id, remote(item("bad")), run_id=run_id)
    for file_id in (ok_file, bad_file):
        assert await repo.begin_file(file_id, run_id)
    assert await repo.complete_file(ok_file, source_id=source_id, account_id=None, entities=entities(1, "m"), result={},
                                    warnings=[], is_valid=True, content_sha256=None)
    assert await repo.fail_file(bad_file, "persist", "Could not store the entities in the CRM store: ORA-01653")

    assert run_id in {int(r["id"]) for r in await repo.stuck_runs(ANCIENT + timedelta(days=1))}
    assert await repo.finish_run(run_id, worker_id=WORKER, status="PARTIAL", error_message="1 of 2 files failed",
                                 counts={"files_failed": 1}, details={}, source_id=source_id, next_sync_at=None,
                                 schedule_interval_days=None, schedule_hour=None)
    since = utc_now() - timedelta(hours=1)
    assert bad_file in {int(r["id"]) for r in await repo.failed_files_since(since)}
    assert (await repo.persist_failures([run_id]))[run_id]["n"] == 1
    assert int((await repo.last_runs())[source_id]["id"]) == run_id
    assert run_id in {int(r["id"]) for r in await repo.run_activity(500)}
    assert {ok_file, bad_file} <= {int(r["id"]) for r in await repo.file_activity(500)}
    failed_run = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
    await force_running(db, failed_run)
    await repo.finish_run(failed_run, worker_id=WORKER, status="FAILED", error_message="AADSTS7000215", counts={},
                          details={}, source_id=source_id, next_sync_at=None, schedule_interval_days=None, schedule_hour=None)
    assert failed_run in {int(r["id"]) for r in await repo.failed_runs_since(since)}
    stats = await repo.store_stats()
    assert stats["sources"] >= 1 and stats["entities_active"] >= 1 and stats["files_active"] >= 2
    rows, total = await repo.list_files(source_id, status="FAILED")
    assert total == 1 and int(rows[0]["id"]) == bad_file
    counts = (await repo.counts_by_source())[source_id]
    assert counts == {"files_active": 2, "files_failed": 1, "files_deleted": 0, "entities_active": 1}


# ---- a whole sync run on real SQL (review Phase 2) ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_sync_runs_end_to_end_on_oracle(db: TxDatabase, tmp_path):
    """SyncService.process_run with scripted Microsoft Graph and extraction fakes, on this
    run's rows only: every CrmRepo statement of a sync in its real order (listing diff, upserts,
    live progress, the five stages, entity replacement, deletion + withdrawal, rename, failure,
    finish). Audit and error-log writes are stubbed: nothing reaches another table."""
    from backend.doc_intel.crm_extraction import CrmExtractionFailed
    from backend.doc_intel.crm_sync import SyncService
    from backend.doc_intel.settings import DocIntelSettings

    from ._crm_fakes import FakeCrmExtractor, FakeGraph, FakeGraphFactory

    repo, box = CrmRepo(db), FakeBox()
    source_id = await new_source(repo, box, "e2e")
    settings = DocIntelSettings(_env_file=None, timezone="Africa/Cairo", audit_enabled=False, error_log_enabled=False)
    graph, extractor, stubbed = FakeGraph(), FakeCrmExtractor(), []

    async def no_write(**kw: Any) -> None:
        stubbed.append(kw)

    service = SyncService(
        db=db, repo=repo, settings=settings, box_factory=lambda: box, graph_factory=FakeGraphFactory(graph),
        extract_fn=extractor, audit=no_write, error_log=no_write, temp_root=tmp_path,
    )

    async def run_once() -> tuple[int, str]:
        run_id = await repo.create_run(source_id, trigger_type="MANUAL", triggered_by=MARKER)
        await force_running(db, run_id)
        return run_id, await service.process_run(await repo.get_run(run_id))

    def file_row(name: str) -> dict[str, Any]:
        return next(r for r in rows if r["item_id"] == item(name))

    # Run 1: three new files, all processed.
    graph.files = [remote(item("a")), remote(item("b")), remote(item("d"))]
    first, status = await run_once()
    assert status == "COMPLETED", (await repo.get_run(first))["error_message"]
    run = await repo.get_run(first)
    assert (run["files_seen"], run["files_new"], run["files_failed"]) == (3, 3, 0)
    assert loads_json(run["details_json"], {})["files_processed"] == 3  # set_run_progress
    rows, total = await repo.list_files(source_id)
    assert total == 3 and {r["status"] for r in rows} == {"COMPLETED"}
    assert all(file_to_out(r).stages[-1].status == "COMPLETED" for r in rows)
    entities_a1 = {int(e["id"]) for e in (await repo.list_entities(source_id=source_id, status="ACTIVE"))[0]
                   if int(e["source_file_id"]) == int(file_row("a")["id"])}
    assert len(entities_a1) == 2 and (await repo.get_source(source_id))["resolved_folder_id"] == "folder-1"

    # Run 2: a changed, b deleted, c new (fails in the child's intelligence stage), d renamed.
    graph.files = [remote(item("a"), ctag="c2"), remote(item("c")), remote(item("d"), name="renamed.pdf")]
    extractor.errors[f"{item('c')}.pdf"] = CrmExtractionFailed(
        "CRM entity extraction failed: bad table", stage="intelligence", code="entity_extraction_failed"
    )
    second, status = await run_once()
    run = await repo.get_run(second)
    assert status == "PARTIAL" and run["error_message"] == "1 of 2 files failed"
    assert (run["files_new"], run["files_changed"], run["files_deleted"], run["files_unchanged"]) == (1, 1, 1, 1)
    rows, _ = await repo.list_files(source_id)
    a, b, c, d = (file_row(n) for n in ("a", "b", "c", "d"))
    assert a["status"] == "COMPLETED" and a["ctag"] == "c2" and b["state"] == "DELETED" and b["deleted_at"]
    out_c = file_to_out(c)
    assert (out_c.status, out_c.failed_stage) == ("FAILED", "intelligence")
    assert [s.status for s in out_c.stages] == ["COMPLETED", "COMPLETED", "FAILED", "PENDING", "PENDING"]
    assert d["name"] == "renamed.pdf" and d["status"] == "COMPLETED" and d["last_seen_at"]
    active, _ = await repo.list_entities(source_id=source_id, status="ACTIVE")
    by_file: dict[int, set[int]] = {}
    for e in active:
        by_file.setdefault(int(e["source_file_id"]), set()).add(int(e["id"]))
    assert int(b["id"]) not in by_file and len(by_file[int(a["id"])]) == 2 and entities_a1.isdisjoint(by_file[int(a["id"])])
    withdrawn, _ = await repo.list_entities(source_id=source_id, status="WITHDRAWN")
    assert {int(e["id"]) for e in withdrawn} >= entities_a1 and all(e["withdrawn_at"] for e in withdrawn)
    assert (await repo.counts_by_source(source_id))[source_id] == {
        "files_active": 3, "files_failed": 1, "files_deleted": 1, "entities_active": 4,
    }
    source = await repo.get_source(source_id)
    assert source["last_sync_status"] == "PARTIAL" and source["last_success_at"] is not None
    assert stubbed == [] and list(tmp_path.iterdir()) == []  # nothing audited; every download dir removed


# ---- isolation check (keep last) --------------------------------------------------------------------------------


def test_zz_this_run_left_no_rows_behind(oracle_crm: dict[str, Any], capsys: pytest.CaptureFixture[str]):
    """Counted from a fresh session, which only sees committed data."""
    ids = {f"s{i}": sid for i, sid in enumerate(SOURCE_IDS)}
    in_list = ", ".join(f":{k}" for k in ids) or "NULL"
    marker = {"m": MARKER}
    queries = {
        "AIVA_CRM_SOURCES": (
            f"SELECT COUNT(*) FROM AIVA_crm_sources WHERE name LIKE :p OR created_by = :m OR id IN ({in_list})",
            {"p": f"{NAME_PREFIX}%", **marker, **ids},
        ),
        "AIVA_CRM_SYNC_RUNS": (
            f"SELECT COUNT(*) FROM AIVA_crm_sync_runs WHERE triggered_by = :m OR source_id IN ({in_list})", {**marker, **ids},
        ),
        "AIVA_CRM_SOURCE_FILES": (
            f"SELECT COUNT(*) FROM AIVA_crm_source_files WHERE item_id LIKE :p OR source_id IN ({in_list})",
            {"p": f"{ITEM_PREFIX}%", **ids},
        ),
        "AIVA_CRM_ENTITIES": (
            f"SELECT COUNT(*) FROM AIVA_crm_entities WHERE match_key LIKE :p OR account_id = :m OR source_id IN ({in_list})",
            {"p": f"%{ITEM_PREFIX}%", **marker, **ids},
        ),
    }
    conn = oracledb.connect(**oracle_crm)
    try:
        counts = {}
        with conn.cursor() as cur:
            for table, (sql, binds) in queries.items():
                cur.execute(sql, binds)
                counts[table] = int(cur.fetchone()[0])
    finally:
        conn.close()
    with capsys.disabled():
        print(
            f"\n[doc-intel CRM IT] run {RUN}: marker {MARKER}, {len(SOURCE_IDS)} sources created and rolled back; "
            f"rows left: {counts}"
        )
    assert counts == dict.fromkeys(queries, 0)
