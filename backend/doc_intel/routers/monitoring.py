"""Module status and monitoring endpoints (Super Admin + Developer)."""
from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.doc_intel.constants import ALLOWED_EXTENSIONS, HEALTH_COMPONENT_LABELS
from backend.doc_intel.guards import MonitorUser
from backend.doc_intel.health import load_activity, load_events, load_failures, load_overview, run_checks
from backend.doc_intel.runtime import NOT_INSTALLED_DETAIL, NOT_STARTED_DETAIL, DocIntelRuntime, InstalledRuntime, get_runtime
from backend.doc_intel.schemas import (
    ActivityOut,
    DocIntelStatusOut,
    FailuresOut,
    HealthEventOut,
    HealthOverviewOut,
)
from backend.doc_intel.settings import DocIntelSettings
from backend.exceptions import BadRequestError

_log = logging.getLogger(__name__)

router = APIRouter(tags=["doc-intel"])


@router.get("/status", response_model=DocIntelStatusOut)
async def doc_intel_status(
    user: MonitorUser,
    runtime: Annotated[DocIntelRuntime | None, Depends(get_runtime)],
) -> DocIntelStatusOut:
    """Whether the module is installed and running, plus upload limits (works when not installed)."""
    if runtime is None:
        return DocIntelStatusOut(enabled=True, installed=False, detail=NOT_STARTED_DETAIL)
    settings = runtime.settings
    available, why = False, None
    if runtime.extraction is not None:
        try:
            available, why = await asyncio.to_thread(runtime.extraction.extraction_available)
        except Exception as ex:
            _log.warning("doc_intel: extraction_available() failed", exc_info=True)
            available, why = False, f"Extraction check failed: {type(ex).__name__}"
    else:
        why = "Extraction is not initialised"
    detail = runtime.detail
    if not runtime.installed:
        detail = NOT_INSTALLED_DETAIL + (f" ({runtime.detail})" if runtime.detail else "")
    return DocIntelStatusOut(
        enabled=settings.enabled,
        installed=runtime.installed,
        schema_version=runtime.schema_version,
        worker_running=runtime.worker_running,
        extraction_available=bool(available),
        extraction_unavailable_reason=None if available else why,
        detail=detail,
        max_upload_mb=settings.max_upload_mb,
        max_files_per_upload=settings.max_files_per_upload,
        allowed_extensions=list(ALLOWED_EXTENSIONS),
        crm_installed=runtime.crm_installed,
        sync_worker_running=runtime.sync_worker_running,
        scheduler_enabled=settings.scheduler_enabled,
        secrets_key_configured=_secrets_key_configured(settings),
    )


def _secrets_key_configured(settings: DocIntelSettings) -> bool:
    """Whether DOC_INTEL_SECRETS_KEY holds a usable key (never raises; the key is never shown)."""
    try:
        from backend.doc_intel.crypto import secrets_configured

        return bool(secrets_configured(settings))
    except Exception:
        _log.warning("doc_intel: could not check DOC_INTEL_SECRETS_KEY", exc_info=True)
        return False


@router.get("/monitoring/health", response_model=HealthOverviewOut)
async def monitoring_health(user: MonitorUser, runtime: InstalledRuntime) -> HealthOverviewOut:
    """Stored results of the last checks (never runs a check)."""
    return await load_overview(runtime.health)


@router.post("/monitoring/health/run", response_model=HealthOverviewOut)
async def monitoring_health_run(
    user: MonitorUser,
    runtime: InstalledRuntime,
    component: str | None = Query(default=None, max_length=64),
) -> HealthOverviewOut:
    """Run the checks now (all, or one ``component``); at most once per 30 s, else ``throttled``."""
    if component and component not in HEALTH_COMPONENT_LABELS:
        raise BadRequestError(f"Unknown component: {component}")
    return await run_checks(runtime.health, component=component or None)


@router.get("/monitoring/events", response_model=list[HealthEventOut])
async def monitoring_events(
    user: MonitorUser,
    runtime: InstalledRuntime,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[HealthEventOut]:
    """Component status transitions, newest first."""
    return await load_events(runtime.health, limit)


@router.get("/monitoring/failures", response_model=FailuresOut)
async def monitoring_failures(
    user: MonitorUser,
    runtime: InstalledRuntime,
    days: int = Query(default=7, ge=1, le=90),
) -> FailuresOut:
    """Documents (and, once SharePoint sync is installed, files and sync runs) that failed in the
    last ``days`` days, newest first."""
    return await load_failures(runtime.health, days)


@router.get("/monitoring/activity", response_model=ActivityOut)
async def monitoring_activity(
    user: MonitorUser,
    runtime: InstalledRuntime,
    limit: int = Query(default=100, ge=1, le=500),
) -> ActivityOut:
    """Document import (and SharePoint sync) activity and health transitions, newest first."""
    return await load_activity(runtime.health, limit)
