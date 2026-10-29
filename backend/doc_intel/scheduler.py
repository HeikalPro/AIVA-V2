"""The document-intelligence scheduler: automatic SharePoint syncs and periodic health checks.

An asyncio ticker started from the app lifespan only when ``DOC_INTEL_SCHEDULER_ENABLED=true``
(off by default), every ``DOC_INTEL_SCHEDULER_TICK_SECONDS``. Each tick:

- **Due syncs** (migration V002): every ACTIVE source whose schedule is on and whose
  ``next_sync_at`` has passed is claimed optimistically (``UPDATE ... SET next_sync_at = :new
  WHERE id = :id AND next_sync_at <= :now``, accepted only when exactly one row changed; the
  first claim moves ``next_sync_at`` past ``now``), so a second process or replica cannot queue
  the same due time twice. The claim moves ``next_sync_at`` to the next occurrence; a SCHEDULED
  run is then queued and the sync worker woken. A run already queued or running for the source covers the schedule; when the run
  cannot be queued at all, the claim is undone so the next tick retries.
- **Health**: every check runs when the newest stored result is older than
  ``DOC_INTEL_HEALTH_INTERVAL_MINUTES``. The stored results are shared, so replicas do not
  repeat each other's checks.

A tick never raises: problems are logged and the next tick tries again.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from backend.doc_intel import schedule
from backend.doc_intel.crm_repo import TRIGGER_SCHEDULED, RunAlreadyActive
from backend.doc_intel.health import HealthDeps, run_checks
from backend.doc_intel.kb_repo import parse_utc
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.textutil import utc_now

_log = logging.getLogger(__name__)

INITIAL_DELAY_SECONDS = 10.0  # let the app finish starting before the first tick


class DocIntelScheduler:
    def __init__(
        self,
        *,
        settings: DocIntelSettings,
        health_deps: HealthDeps | None = None,
        crm_repo: Any = None,
        wake_sync: Callable[[], None] | None = None,
        health_runner: Callable[[HealthDeps], Awaitable[Any]] | None = None,
        tick_seconds: float | None = None,
        initial_delay_seconds: float = INITIAL_DELAY_SECONDS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._health_deps = health_deps
        self._repo = crm_repo
        self._wake_sync = wake_sync
        self._health_runner = health_runner or run_checks
        self._tick_seconds = float(tick_seconds if tick_seconds is not None else settings.scheduler_tick_seconds)
        self._initial_delay = max(0.0, float(initial_delay_seconds))
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start ticking on the running event loop (idempotent)."""
        if self.running:
            return
        self._task = asyncio.get_running_loop().create_task(self._run(), name="doc-intel-scheduler")
        _log.info(
            "doc_intel: scheduler started (every %gs; SharePoint syncs %s)",
            self._tick_seconds,
            "on" if self._repo is not None else "not installed",
        )

    async def stop(self, timeout: float = 5.0) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout)
        except Exception:
            _log.warning("doc_intel: scheduler did not stop within %ss", timeout)

    async def tick(self) -> dict[str, Any]:
        """One pass (also used by tests). Never raises."""
        now = self._clock()
        summary: dict[str, Any] = {"queued": [], "lost": [], "already_active": [], "failed": [], "health": False}
        if self._repo is not None:
            try:
                await self._queue_due_syncs(now, summary)
            except Exception:
                _log.exception("doc_intel: scheduler could not read the due SharePoint syncs")
        if self._health_deps is not None:
            try:
                summary["health"] = await self._run_health_if_due(now)
            except Exception:
                _log.exception("doc_intel: scheduled health checks failed")
        return summary

    async def _queue_due_syncs(self, now: datetime, summary: dict[str, Any]) -> None:
        for row in await self._repo.due_sources(now):
            source_id = int(row["id"])
            try:
                await self._queue_one(row, now, summary)
            except Exception:
                summary["failed"].append(source_id)
                _log.exception("doc_intel: scheduled sync of source %s was not queued", source_id)
        if summary["queued"] and self._wake_sync is not None:
            try:
                self._wake_sync()
            except Exception:
                _log.warning("doc_intel: could not wake the sync worker", exc_info=True)

    async def _queue_one(self, row: dict[str, Any], now: datetime, summary: dict[str, Any]) -> None:
        source_id = int(row["id"])
        old = parse_utc(row.get("next_sync_at"))
        if old is None:
            return
        hour = row.get("sync_hour")
        new = schedule.next_after_run(
            now,
            interval_days=int(row.get("sync_interval_days") or 14),
            hour=int(hour) if hour is not None else 2,
            tz_name=self._settings.timezone,
        )
        # Claimed on "still due at now", never on equality with ``old`` (review finding F24).
        if not await self._repo.claim_due_source(source_id, now=now, new=new):
            summary["lost"].append(source_id)  # another process claimed it (or the schedule changed)
            return
        try:
            await self._repo.create_run(source_id, trigger_type=TRIGGER_SCHEDULED, triggered_by=None)
        except RunAlreadyActive:
            summary["already_active"].append(source_id)
            _log.info("doc_intel: scheduled sync of source %s skipped: a sync is already queued or running", source_id)
            return
        except Exception:
            summary["failed"].append(source_id)
            _log.exception("doc_intel: could not queue the scheduled sync of source %s; retrying next tick", source_id)
            try:
                await self._repo.restore_next_sync(source_id, expected=new, value=old)
            except Exception:
                _log.warning("doc_intel: could not undo the schedule claim of source %s", source_id, exc_info=True)
            return
        summary["queued"].append(source_id)
        _log.info("doc_intel: scheduled sync of source %s queued (next after this: %s)", source_id, new.isoformat())

    async def _run_health_if_due(self, now: datetime) -> bool:
        deps = self._health_deps
        assert deps is not None
        rows = await deps.health_repo.load_all()
        stamps = [t for t in (parse_utc(r.get("checked_at")) for r in rows) if t is not None]
        newest = max(stamps) if stamps else None
        if newest is not None and newest > now - timedelta(minutes=self._settings.health_interval_minutes):
            return False
        await self._health_runner(deps)
        return True

    async def _run(self) -> None:
        if self._initial_delay:
            await asyncio.sleep(self._initial_delay)
        while True:
            await self.tick()
            await asyncio.sleep(self._tick_seconds)
