from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep
from backend.schemas.http_logs import HttpRequestLogListOut, HttpRequestLogOut
from backend.schemas.logs import AuditLogListOut, AuditLogOut, SignInLogListOut, SignInLogOut
from backend.services.http_request_log import list_http_request_logs
from backend.services.log_queries import list_audit_logs, list_sign_in_logs

router = APIRouter(prefix="/logs", tags=["logs"])

_ACTIVITY_ACCESS = require_roles_or_nav_permission(
    "logs",
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_SUPERVISOR,
)
_SIGN_IN_ACCESS = require_roles_or_nav_permission(
    "logs",
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
)
_API_ACCESS = require_roles_or_nav_permission(
    "logs",
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_DEVELOPER,
)


def _http_summary(row: dict) -> str:
    method = row.get("http_method") or "?"
    handler = row.get("handler_name") or "unknown"
    email = row.get("user_email") or row.get("actor_label") or "anonymous"
    status = row.get("status_code")
    ms = row.get("duration_ms")
    return f"{method} {handler} — {email} — {status} — {ms}ms"


@router.get("/activity", response_model=AuditLogListOut)
async def activity_logs(
    user: Annotated[UserContext, Depends(_ACTIVITY_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action_type: str | None = Query(default=None, max_length=64),
    entity_type: str | None = Query(default=None, max_length=64),
    account_id: int | None = Query(default=None),
) -> AuditLogListOut:
    rows = await list_audit_logs(
        db,
        user,
        limit=limit,
        offset=offset,
        action_type=action_type,
        entity_type=entity_type,
        account_id=account_id,
    )
    return AuditLogListOut(
        items=[AuditLogOut(**row) for row in rows],
        limit=limit,
        offset=offset,
    )


@router.get("/sign-in", response_model=SignInLogListOut)
async def sign_in_logs(
    user: Annotated[UserContext, Depends(_SIGN_IN_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    event_type: str | None = Query(default=None, max_length=100),
) -> SignInLogListOut:
    rows = await list_sign_in_logs(
        db,
        user,
        limit=limit,
        offset=offset,
        event_type=event_type,
    )
    return SignInLogListOut(
        items=[SignInLogOut(**row) for row in rows],
        limit=limit,
        offset=offset,
    )


@router.get("/api", response_model=HttpRequestLogListOut)
async def api_logs(
    user: Annotated[UserContext, Depends(_API_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    method: str | None = Query(default=None, max_length=10),
) -> HttpRequestLogListOut:
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
    items = []
    for row in rows:
        data = dict(row)
        data["summary"] = _http_summary(data)
        items.append(HttpRequestLogOut(**data))
    return HttpRequestLogListOut(items=items, limit=limit, offset=offset)
