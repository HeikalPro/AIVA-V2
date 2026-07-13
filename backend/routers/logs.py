from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query

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
from backend.config import get_settings
from backend.schemas.logs import (
    AiRequestListOut,
    AiRequestOut,
    AuditLogListOut,
    AuditLogOut,
    RagRetrievalListOut,
    RagRetrievalOut,
    SignInLogListOut,
    SignInLogOut,
    WidgetTurnAck,
    WidgetTurnIn,
)
from backend.services.ai_request_log import persist_ai_request
from backend.services.http_request_log import list_http_request_logs
from backend.services.llm_cost import estimate_llm_cost_usd
from backend.services.log_queries import list_ai_requests, list_audit_logs, list_sign_in_logs
from backend.services.rag_retrieval_log import list_rag_retrievals, persist_rag_retrieval

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
_AI_ACCESS = require_roles_or_nav_permission(
    "logs",
    ROLE_SUPER_ADMIN,
    ROLE_ORG_ADMIN,
    ROLE_DEVELOPER,
)


def _org_scope(user: UserContext) -> int | None:
    """None = see all (super admin / developer); otherwise restrict to own org."""
    if user.is_super_admin or user.has_role(ROLE_DEVELOPER):
        return None
    if user.is_org_admin:
        return user.organization_id
    return user.organization_id


def _http_summary(row: dict) -> str:
    method = row.get("http_method") or "?"
    path = row.get("path") or row.get("route_template") or "unknown path"
    email = row.get("user_email") or row.get("actor_label") or "anonymous"
    status = row.get("status_code")
    ms = row.get("duration_ms")
    return f"{method} {path} — {email} — {status} — {ms}ms"


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


@router.get("/rag", response_model=RagRetrievalListOut)
async def rag_logs(
    user: Annotated[UserContext, Depends(_AI_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: int | None = Query(default=None),
    status: str | None = Query(default=None, max_length=16),
) -> RagRetrievalListOut:
    rows = await list_rag_retrievals(
        db,
        org_id=_org_scope(user),
        account_id=account_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return RagRetrievalListOut(
        items=[RagRetrievalOut(**row) for row in rows],
        limit=limit,
        offset=offset,
    )


@router.get("/ai-requests", response_model=AiRequestListOut)
async def ai_request_logs(
    user: Annotated[UserContext, Depends(_AI_ACCESS)],
    db: DbDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    account_id: int | None = Query(default=None),
    status: str | None = Query(default=None, max_length=16),
) -> AiRequestListOut:
    rows = await list_ai_requests(
        db,
        org_id=_org_scope(user),
        account_id=account_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return AiRequestListOut(
        items=[AiRequestOut(**row) for row in rows],
        limit=limit,
        offset=offset,
    )


@router.post("/widget-turn", response_model=WidgetTurnAck)
async def log_widget_turn(
    body: WidgetTurnIn,
    db: DbDep,
    x_widget_log_secret: Annotated[str | None, Header()] = None,
) -> WidgetTurnAck:
    """Ingest one widget conversation turn for logging (machine-to-machine).

    Guarded by a shared secret. Writes are best-effort: even if persistence
    fails, this returns ok so the widget never treats logging as a hard error.
    """
    secret = get_settings().widget_log_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Widget logging is disabled")
    if x_widget_log_secret != secret:
        raise HTTPException(status_code=401, detail="Invalid widget log secret")

    # Resolve the account (and org, for UI scoping) from the corpus the widget used.
    account_id: int | None = None
    if body.corpus_id:
        acct = await db.fetch_one(
            "SELECT id FROM AIVA_accounts WHERE corpus_id = :corpus_id",
            {"corpus_id": body.corpus_id},
        )
        if acct:
            account_id = int(acct["id"])

    chunks = [c.model_dump() for c in body.chunks]
    if body.retrieval_status:
        status = body.retrieval_status.upper()
    elif body.retrieval_error:
        status = "FAILED"
    elif chunks:
        status = "SUCCESS"
    else:
        status = "EMPTY"

    await persist_rag_retrieval(
        db,
        session_id=None,
        account_id=account_id,
        corpus_id=body.corpus_id,
        query_text=body.query_text,
        top_k=body.top_k,
        verticals=body.verticals,
        status=status,
        chunks=chunks,
        retrieval_ms=body.retrieval_ms,
        error_message=body.retrieval_error,
        source="WIDGET",
    )

    total_cost = body.total_cost
    if total_cost is None and body.model_name:
        total_cost = estimate_llm_cost_usd(
            model_name=body.model_name,
            input_tokens=body.input_tokens,
            output_tokens=body.output_tokens,
        )
    await persist_ai_request(
        db,
        session_id=None,
        account_id=account_id,
        model_name=body.model_name,
        provider=body.provider,
        input_tokens=body.input_tokens,
        output_tokens=body.output_tokens,
        response_time_ms=body.response_time_ms,
        total_cost=total_cost,
        status="FAILED" if body.llm_error else "SUCCESS",
        error_message=body.llm_error,
        source="WIDGET",
    )
    return WidgetTurnAck(ok=True)
