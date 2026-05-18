from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_account_access,
    require_roles,
)
from backend.dependencies import DbDep
from backend.exceptions import NotFoundError
from backend.schemas.analytics import AgentMetricOut, DashboardStats
from backend.utils import serialize_row

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard_stats(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR)),
    ],
    db: DbDep,
    account_id: int = Query(...),
) -> DashboardStats:
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(account["organization_id"]))

    row = await db.fetch_one(
        """
        SELECT
            COUNT(DISTINCT cs.id) AS total_sessions,
            COUNT(cm.id) AS total_messages,
            COUNT(ar.id) AS total_ai_requests,
            AVG(ar.response_time_ms) AS avg_response_time_ms,
            NVL(SUM(ar.input_tokens), 0) AS total_input_tokens,
            NVL(SUM(ar.output_tokens), 0) AS total_output_tokens,
            SUM(ar.total_cost) AS total_cost
        FROM AIVA_chat_sessions cs
        LEFT JOIN AIVA_chat_messages cm ON cm.session_id = cs.id
        LEFT JOIN AIVA_ai_requests ar ON ar.session_id = cs.id
        WHERE cs.account_id = :account_id
        """,
        {"account_id": account_id},
    )
    data = serialize_row(row) or {}
    return DashboardStats(
        total_sessions=int(data.get("total_sessions") or 0),
        total_messages=int(data.get("total_messages") or 0),
        total_ai_requests=int(data.get("total_ai_requests") or 0),
        avg_response_time_ms=float(data["avg_response_time_ms"]) if data.get("avg_response_time_ms") is not None else None,
        total_input_tokens=int(data.get("total_input_tokens") or 0),
        total_output_tokens=int(data.get("total_output_tokens") or 0),
        total_cost=float(data["total_cost"]) if data.get("total_cost") is not None else None,
    )


@router.get("/agents", response_model=list[AgentMetricOut])
async def agent_metrics(
    user: Annotated[
        UserContext,
        Depends(require_roles(ROLE_SUPER_ADMIN, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR)),
    ],
    db: DbDep,
    account_id: int = Query(...),
) -> list[AgentMetricOut]:
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(account["organization_id"]))

    rows = await db.fetch_all(
        """
        SELECT user_id, account_id, avg_response_time, ai_usage_count,
               successful_answers, escalation_count, calculated_at
        FROM AIVA_agent_performance_metrics
        WHERE account_id = :account_id
        ORDER BY calculated_at DESC
        """,
        {"account_id": account_id},
    )
    return [AgentMetricOut(**serialize_row(r) or {}) for r in rows]
