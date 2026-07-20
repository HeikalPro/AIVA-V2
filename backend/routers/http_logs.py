from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep
from backend.schemas.http_logs import (
    HttpRequestLogListOut,
    HttpRequestLogOut,
    HttpRequestStatsOut,
)
from backend.services.http_request_log import get_http_request_stats, list_http_request_logs

router = APIRouter(prefix="/http-logs", tags=["http-logs"])

_ACCESS = require_roles_or_nav_permission("http-logs", ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_DEVELOPER)


def _org_scope(user: UserContext) -> int | None:
    """Super admins and developers see everything; org admins see their org only."""
    if not user.is_super_admin and not user.has_role(ROLE_DEVELOPER) and user.is_org_admin:
        return user.organization_id
    return None


def _summary(row: dict) -> str:
    method = row.get("http_method") or "?"
    handler = row.get("handler_name") or "unknown"
    email = row.get("user_email") or row.get("actor_label") or "anonymous"
    status = row.get("status_code")
    ms = row.get("duration_ms")
    return f"{method} {handler} — {email} — {status} — {ms}ms"


def _to_out(row: dict) -> HttpRequestLogOut:
    data = dict(row)
    data["summary"] = _summary(data)
    return HttpRequestLogOut(**data)


@router.get("", response_model=HttpRequestLogListOut)
async def list_logs(
    user: Annotated[UserContext, Depends(_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    method: str | None = Query(default=None, max_length=10),
) -> HttpRequestLogListOut:
    """Explainable HTTP handler logs. Org admins see their organization only."""
    rows = await list_http_request_logs(
        db,
        org_id=_org_scope(user),
        limit=limit,
        offset=offset,
        http_method=method,
    )
    return HttpRequestLogListOut(
        items=[_to_out(r) for r in rows],
        limit=limit,
        offset=offset,
    )


@router.get("/stats", response_model=HttpRequestStatsOut)
async def request_stats(
    user: Annotated[UserContext, Depends(_ACCESS)],
    db: DbDep,
    start: str | None = Query(default=None, max_length=10),
    end: str | None = Query(default=None, max_length=10),
) -> HttpRequestStatsOut:
    """Aggregate request counts per endpoint and per user. Org admins see their organization only."""
    data = await get_http_request_stats(
        db,
        org_id=_org_scope(user),
        start=start,
        end=end,
    )
    return HttpRequestStatsOut(**data)
