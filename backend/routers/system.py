import asyncio
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_DEVELOPER,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep, EmbeddingServiceDep
from backend.config import get_settings
from backend.schemas.system import (
    ComponentHealth,
    ComponentStatus,
    RedisStatus,
    ResourceStats,
    SystemComponentsOut,
    SystemHealthOut,
    SystemResourcesOut,
    TrafficStats,
)
from backend.services import system_health as sh
from backend.services.sse_metrics import active_sse_connections
from backend.services.system_resources import resource_snapshot

router = APIRouter(prefix="/system", tags=["system"])

_ACCESS = require_roles_or_nav_permission("system", ROLE_SUPER_ADMIN, ROLE_DEVELOPER)


@router.get("/health", response_model=SystemHealthOut)
async def system_health(
    user: Annotated[UserContext, Depends(_ACCESS)],
    db: DbDep,
    embedding_svc: EmbeddingServiceDep,
    window_minutes: int = Query(default=5, ge=1, le=60),
) -> SystemHealthOut:
    """Operations snapshot: infra status + recent traffic metrics."""
    redis_url = getattr(embedding_svc.settings, "redis_url", None)
    queue_name = getattr(embedding_svc.settings, "redis_job_queue", "embedding_service:jobs")

    database = await sh.db_health(db)
    redis = await sh.redis_snapshot(redis_url, queue_name)
    resources = await asyncio.to_thread(sh.system_resources)
    traffic = await sh.http_traffic_stats(db, window_minutes)
    sse = active_sse_connections()

    overall = "ok"
    if database["status"] == "down":
        overall = "down"
    elif redis["status"] == "down" or (traffic.get("error_rate") or 0) > 0.1:
        overall = "degraded"

    return SystemHealthOut(
        status=overall,
        generated_at=datetime.now(timezone.utc).isoformat(),
        database=ComponentStatus(**database),
        redis=RedisStatus(**redis),
        resources=ResourceStats(**resources),
        sse_connections=sse,
        traffic=TrafficStats(**traffic),
    )


@router.get("/resources", response_model=SystemResourcesOut)
async def system_resources(
    user: Annotated[UserContext, Depends(_ACCESS)],
) -> SystemResourcesOut:
    """Detailed host + backend-process resource snapshot (CPU, memory, disk, network)."""
    snap = await asyncio.to_thread(resource_snapshot)
    return SystemResourcesOut(**snap)


@router.get("/components", response_model=SystemComponentsOut)
async def system_components(
    user: Annotated[UserContext, Depends(_ACCESS)],
    db: DbDep,
) -> SystemComponentsOut:
    """Reachability of external dependencies: Oracle DB and the LLM provider."""
    settings = get_settings()

    database, llm = await asyncio.gather(
        sh.db_health(db),
        sh.llm_health(),
    )

    components = [
        ComponentHealth(
            key="database",
            label="Database",
            status=database.get("status", "unknown"),
            latency_ms=database.get("latency_ms"),
            detail=database.get("detail"),
            info=settings.oracle_dsn,
        ),
        ComponentHealth(
            key="llm",
            label="LLM",
            status=llm.get("status", "unknown"),
            latency_ms=llm.get("latency_ms"),
            detail=llm.get("detail"),
            info=llm.get("info"),
        ),
    ]

    return SystemComponentsOut(
        generated_at=datetime.now(timezone.utc).isoformat(),
        components=components,
    )
