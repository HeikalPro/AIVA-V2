from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    UserContext,
    require_roles,
)
from backend.dependencies import DbDep
from backend.schemas.http_logs import HttpRequestLogListOut, HttpRequestLogOut
from backend.services.http_request_log import list_http_request_logs

router = APIRouter(prefix="/http-logs", tags=["http-logs"])


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
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_DEVELOPER)),
    ],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    method: str | None = Query(default=None, max_length=10),
) -> HttpRequestLogListOut:
    """Explainable HTTP handler logs. Org admins see their organization only."""
    org_filter: int | None = None
    if not user.is_super_admin and not user.has_role(ROLE_DEVELOPER):
        if user.is_org_admin:
            org_filter = user.organization_id

    rows = await list_http_request_logs(
        db,
        org_id=org_filter,
        limit=limit,
        offset=offset,
        http_method=method,
    )
    return HttpRequestLogListOut(
        items=[_to_out(r) for r in rows],
        limit=limit,
        offset=offset,
    )
