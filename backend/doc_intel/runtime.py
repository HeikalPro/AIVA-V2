"""Lifecycle and FastAPI dependencies of the document intelligence module.

``start_doc_intel`` / ``stop_doc_intel`` are called from the app lifespan and NEVER
raise: whatever goes wrong here (settings, missing tables, an import error) disables
only this module, and the rest of AIVA starts normally. No DDL runs here; each install
check (V001, then V002) is two read-only SELECTs.

Phase 2 (SharePoint -> CRM, migration V002) starts only when its tables are installed, and
the scheduler only when ``DOC_INTEL_SCHEDULER_ENABLED=true``; a problem in either leaves
Phase 1 (knowledge-document import and monitoring) running.

Module-level imports are deliberately light: the pipeline modules are imported inside
``start_doc_intel`` so an error in them cannot break importing ``backend.main``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from backend.doc_intel.constants import (
    REQUIRED_TABLES_V001,
    REQUIRED_TABLES_V002,
    SCHEMA_VERSION_V001,
    SCHEMA_VERSION_V002,
    T_SCHEMA_VERSION,
)
from backend.doc_intel.settings import DocIntelSettings, get_doc_intel_settings

if TYPE_CHECKING:
    from fastapi import FastAPI

    from backend.database import Database
    from backend.doc_intel.crm_sync import SyncService, SyncWorker
    from backend.doc_intel.health import HealthDeps
    from backend.doc_intel.kb_import import KbImportService, KbImportWorker
    from backend.doc_intel.kb_store import KbStore
    from backend.doc_intel.scheduler import DocIntelScheduler

_log = logging.getLogger(__name__)

NOT_INSTALLED_DETAIL = "Document intelligence is not installed — run migration V001"
NOT_STARTED_DETAIL = "Document intelligence did not start — check the backend log for 'doc_intel'"
CRM_NOT_INSTALLED_DETAIL = "SharePoint sync is not installed — run migration V002"
CRM_NOT_STARTED_DETAIL = "SharePoint sync did not start — check the backend log for 'doc_intel'"


class ServiceUnavailableError(HTTPException):
    def __init__(self, detail: str = "Service unavailable") -> None:
        super().__init__(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)


@dataclass
class DocIntelRuntime:
    settings: DocIntelSettings
    installed: bool = False
    schema_version: str | None = None
    detail: str | None = None
    service: KbImportService | None = None
    worker: KbImportWorker | None = None
    kb: KbStore | None = None
    health: HealthDeps | None = None
    # Module providing extraction_available() (injectable for tests).
    extraction: Any = None
    # Phase 2: SharePoint / OneDrive -> CRM (migration V002).
    crm_installed: bool = False
    crm_detail: str | None = None
    sync_service: SyncService | None = None
    sync_worker: SyncWorker | None = None
    scheduler: DocIntelScheduler | None = None

    @property
    def worker_running(self) -> bool:
        return bool(self.worker is not None and self.worker.running)

    @property
    def sync_worker_running(self) -> bool:
        return bool(self.sync_worker is not None and self.sync_worker.running)

    @property
    def scheduler_running(self) -> bool:
        return bool(self.scheduler is not None and self.scheduler.running)


async def _check_version(db: Database, tables: tuple[str, ...], version: str) -> tuple[bool, str | None]:
    """Read-only: are ``tables`` present and ``version`` recorded in the ledger? (installed, detail)"""
    names = [t.upper() for t in tables]
    binds = {f"t{i}": name for i, name in enumerate(names)}
    placeholders = ", ".join(f":t{i}" for i in range(len(names)))
    rows = await db.fetch_all(f"SELECT table_name FROM user_tables WHERE table_name IN ({placeholders})", binds)
    found = {str(r.get("table_name") or "").upper() for r in rows}
    missing = [name for name in names if name not in found]
    if missing:
        return False, f"Missing tables: {', '.join(missing)}"
    row = await db.fetch_one(f"SELECT version FROM {T_SCHEMA_VERSION} WHERE version = :version", {"version": version})
    if row is None:
        return False, f"Migration V{version} is not recorded in {T_SCHEMA_VERSION}"
    return True, None


async def check_installed(db: Database) -> tuple[bool, str | None, str | None]:
    """Read-only: are migration V001's tables present and the version recorded?

    Returns (installed, schema_version, detail).
    """
    installed, detail = await _check_version(db, REQUIRED_TABLES_V001, SCHEMA_VERSION_V001)
    return installed, (SCHEMA_VERSION_V001 if installed else None), detail


async def check_crm_installed(db: Database) -> tuple[bool, str | None]:
    """Read-only: are migration V002's tables present and ledger row '002' recorded? (installed, detail)"""
    return await _check_version(db, REQUIRED_TABLES_V002, SCHEMA_VERSION_V002)


async def start_doc_intel(app: FastAPI, db: Database, embedding_svc: Any) -> DocIntelRuntime | None:
    """Check the install, then build the import service/worker and start the worker.

    Returns None when the module is disabled; otherwise the runtime, which is also
    stored on ``app.state.doc_intel`` (installed or not). Never raises.
    """
    try:
        settings = get_doc_intel_settings()
    except Exception:
        _log.exception("doc_intel: invalid DOC_INTEL_* settings; module disabled")
        return None
    if not settings.enabled:
        _log.info("doc_intel: disabled (DOC_INTEL_ENABLED=false)")
        return None

    runtime = DocIntelRuntime(settings=settings)
    try:
        installed, version, detail = await check_installed(db)
        runtime.installed, runtime.schema_version, runtime.detail = installed, version, detail
    except Exception as ex:
        runtime.detail = f"Could not check the doc-intel tables: {type(ex).__name__}"
        _log.exception("doc_intel: install check failed; module disabled")

    if runtime.installed:
        try:
            worker = _build(runtime, db, embedding_svc)
            worker.start()
            _log.info("doc_intel: ready (schema V%s)", runtime.schema_version)
        except Exception as ex:
            runtime.service = runtime.worker = runtime.health = None
            runtime.detail = f"Document intelligence failed to start: {type(ex).__name__}: {ex}"[:500]
            _log.exception("doc_intel: failed to start")
    else:
        _log.warning("doc_intel: not installed — run migration V001 (%s)", runtime.detail or "unknown")

    if runtime.installed and runtime.service is not None:
        try:
            await _start_phase2(runtime, db)
        except Exception:  # _start_phase2 guards each step; this is the backstop
            runtime.crm_installed = False
            _log.exception("doc_intel: SharePoint sync failed to start")

    try:
        app.state.doc_intel = runtime
    except Exception:
        _log.exception("doc_intel: could not attach the runtime to the app")
    return runtime


async def _start_phase2(runtime: DocIntelRuntime, db: Database) -> None:
    """V002 install check, the SharePoint sync service + worker, then the scheduler when enabled.

    Never raises; a failure here leaves Phase 1 running and is reported by the CRM routes (503).
    """
    try:
        runtime.crm_installed, runtime.crm_detail = await check_crm_installed(db)
    except Exception as ex:
        runtime.crm_installed = False
        runtime.crm_detail = f"Could not check the SharePoint sync tables: {type(ex).__name__}"
        _log.exception("doc_intel: SharePoint sync install check failed")

    if runtime.crm_installed:
        try:
            sync_worker = _build_crm(runtime, db)
            sync_worker.start()
            _log.info("doc_intel: SharePoint sync ready (schema V%s)", SCHEMA_VERSION_V002)
        except Exception as ex:
            runtime.sync_service = runtime.sync_worker = None
            _detach_crm_health(runtime)
            runtime.crm_detail = f"SharePoint sync failed to start: {type(ex).__name__}: {ex}"[:500]
            _log.exception("doc_intel: SharePoint sync failed to start")
    else:
        _log.info("doc_intel: SharePoint sync not installed — run migration V002 (%s)", runtime.crm_detail or "unknown")

    if not runtime.settings.scheduler_enabled:
        _log.info("doc_intel: scheduler off (DOC_INTEL_SCHEDULER_ENABLED=false): no automatic syncs or periodic health checks")
        return
    try:
        from backend.doc_intel.scheduler import DocIntelScheduler

        health = runtime.health
        scheduler = DocIntelScheduler(
            settings=runtime.settings,
            health_deps=health,
            crm_repo=health.crm_repo if health is not None and runtime.sync_service is not None else None,
            wake_sync=runtime.sync_worker.wake if runtime.sync_worker is not None else None,
        )
        scheduler.start()
        runtime.scheduler = scheduler
    except Exception:
        runtime.scheduler = None
        _log.exception("doc_intel: the scheduler failed to start")


def _build_crm(runtime: DocIntelRuntime, db: Database) -> SyncWorker:
    from backend.doc_intel import crypto, graph_source
    from backend.doc_intel.crm_repo import CrmRepo
    from backend.doc_intel.crm_sync import SyncService, SyncWorker

    settings = runtime.settings
    repo = CrmRepo(db)
    service = SyncService(db=db, repo=repo, settings=settings)
    worker = SyncWorker(service, settings)
    service.set_wake_callback(worker.wake)
    runtime.sync_service = service
    runtime.sync_worker = worker
    if runtime.health is not None:
        runtime.health.crm_repo = repo
        runtime.health.secret_box_factory = lambda: crypto.SecretBox.from_settings(settings)
        runtime.health.graph_factory = lambda creds: graph_source.GraphSource(creds, settings)
        runtime.health.sync_worker_running = lambda: runtime.sync_worker_running
    return worker


def _detach_crm_health(runtime: DocIntelRuntime) -> None:
    if runtime.health is not None:
        runtime.health.crm_repo = None
        runtime.health.secret_box_factory = None
        runtime.health.graph_factory = None
        runtime.health.sync_worker_running = None


def _build(runtime: DocIntelRuntime, db: Database, embedding_svc: Any) -> KbImportWorker:
    from backend.config import get_settings as get_backend_settings
    from backend.doc_intel import extraction
    from backend.doc_intel.health import HealthDeps, service_name
    from backend.doc_intel.health_repo import HealthRepo
    from backend.doc_intel.kb_import import KbImportService, KbImportWorker
    from backend.doc_intel.kb_repo import KbRepo
    from backend.doc_intel.kb_store import KbStore
    from embedding_service.models.corpus_config import parse_corpus_config
    from embedding_service.services.embedder_factory import make_embedder

    settings = runtime.settings
    emb_settings = embedding_svc.settings

    def embedder_factory(config: dict[str, Any]) -> Any:
        return make_embedder(parse_corpus_config(config), emb_settings)

    kb = KbStore(embedding_svc.db.connection)
    repo = KbRepo(db)
    service = KbImportService(
        db=db,
        repo=repo,
        kb=kb,
        settings=settings,
        embedder_factory=embedder_factory,
        default_price_per_million=getattr(emb_settings, "embedding_default_usd_per_million_tokens", None),
    )
    worker = KbImportWorker(service, settings)
    service.set_wake_callback(worker.wake)

    app_dsn = None
    try:
        app_dsn = get_backend_settings().oracle_dsn
    except Exception:
        _log.debug("doc_intel: backend settings unavailable for the DB service name", exc_info=True)
    runtime.kb = kb
    runtime.service = service
    runtime.worker = worker
    runtime.extraction = extraction
    runtime.health = HealthDeps(
        db=db,
        kb=kb,
        kb_repo=repo,
        health_repo=HealthRepo(db),
        settings=settings,
        embedding_settings=emb_settings,
        worker_running=lambda: runtime.worker_running,
        extraction=extraction,
        app_db_service=service_name(app_dsn),
        kb_db_service=service_name(getattr(emb_settings, "oracle_dsn", None)),
    )
    return worker


async def stop_doc_intel(runtime: DocIntelRuntime | None) -> None:
    """Stop in this order: kill extraction children -> scheduler -> sync worker -> import worker.

    The in-flight document / sync run is marked interrupted (retryable). Never raises.
    """
    if runtime is None or (runtime.worker is None and runtime.sync_worker is None and runtime.scheduler is None):
        return
    # The sync worker takes no new file from here on, so nothing re-spawns an extraction
    # child after the kill below.
    request_stop = getattr(runtime.sync_worker, "request_stop", None)
    if request_stop is not None:
        try:
            request_stop()
        except Exception:
            _log.exception("doc_intel: could not signal the sync worker to stop")
    # Kill an in-flight extraction first: its waiting thread would otherwise hold the
    # shutdown for up to the extraction timeout (and a forced exit orphans the child).
    killed = _kill_extractions(runtime)
    for label, component in (
        ("scheduler", runtime.scheduler),
        ("sync worker", runtime.sync_worker),
        ("import worker", runtime.worker),
    ):
        if component is None:
            continue
        try:
            await component.stop()
        except Exception:
            _log.exception("doc_intel: error while stopping the %s", label)
    if runtime.sync_worker is not None:
        # Two workers share the extraction slot: one whose thread was waiting for it may have
        # started a child after the first kill (the slot freed up when that child died).
        killed += _kill_extractions(runtime)
    _log.info("doc_intel: stopped%s", f" (killed {killed} extraction process)" if killed else "")


def _kill_extractions(runtime: DocIntelRuntime) -> int:
    try:
        if runtime.extraction is not None and hasattr(runtime.extraction, "terminate_active_extractions"):
            return int(runtime.extraction.terminate_active_extractions() or 0)
    except Exception:
        _log.exception("doc_intel: error while killing extraction processes")
    return 0


# ---- FastAPI dependencies ------------------------------------------------------------------


def get_runtime(request: Request) -> DocIntelRuntime | None:
    """The runtime attached at startup, or None when it never started."""
    return getattr(request.app.state, "doc_intel", None)


def require_installed(runtime: Annotated[DocIntelRuntime | None, Depends(get_runtime)]) -> DocIntelRuntime:
    if runtime is None:
        raise ServiceUnavailableError(NOT_STARTED_DETAIL)
    if not runtime.installed:
        raise ServiceUnavailableError(NOT_INSTALLED_DETAIL)
    if runtime.service is None or runtime.health is None:
        raise ServiceUnavailableError(runtime.detail or NOT_STARTED_DETAIL)
    return runtime


InstalledRuntime = Annotated[DocIntelRuntime, Depends(require_installed)]


def require_crm_installed(runtime: InstalledRuntime) -> DocIntelRuntime:
    """The runtime, with SharePoint sync (migration V002) installed and started; else 503."""
    if not runtime.crm_installed:
        raise ServiceUnavailableError(CRM_NOT_INSTALLED_DETAIL)
    if runtime.sync_service is None:
        raise ServiceUnavailableError(runtime.crm_detail or CRM_NOT_STARTED_DETAIL)
    return runtime


CrmRuntime = Annotated[DocIntelRuntime, Depends(require_crm_installed)]
