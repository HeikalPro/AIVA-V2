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
            (SELECT COUNT(*) FROM AIVA_chat_sessions WHERE account_id = :account_id) AS total_sessions,
            (SELECT COUNT(*)
             FROM AIVA_chat_messages cm
             JOIN AIVA_chat_sessions cs ON cs.id = cm.session_id
             WHERE cs.account_id = :account_id) AS total_messages,
            (SELECT COUNT(*)
             FROM AIVA_ai_requests ar
             JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
             WHERE cs.account_id = :account_id) AS total_ai_requests,
            (SELECT AVG(ar.response_time_ms)
             FROM AIVA_ai_requests ar
             JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
             WHERE cs.account_id = :account_id) AS avg_response_time_ms,
            (SELECT NVL(SUM(ar.input_tokens), 0)
             FROM AIVA_ai_requests ar
             JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
             WHERE cs.account_id = :account_id) AS total_input_tokens,
            (SELECT NVL(SUM(ar.output_tokens), 0)
             FROM AIVA_ai_requests ar
             JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
             WHERE cs.account_id = :account_id) AS total_output_tokens,
            (SELECT SUM(ar.total_cost)
             FROM AIVA_ai_requests ar
             JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
             WHERE cs.account_id = :account_id) AS total_cost
        FROM DUAL
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
        SELECT
            cs.user_id,
            :account_id AS account_id,
            u.first_name AS agent_first_name,
            u.last_name AS agent_last_name,
            u.email AS agent_email,
            AVG(ar.response_time_ms) AS avg_response_time,
            COUNT(ar.id) AS ai_usage_count,
            SUM(CASE WHEN UPPER(TRIM(ar.status)) = 'SUCCESS' THEN 1 ELSE 0 END) AS successful_answers,
            0 AS escalation_count,
            MAX(cs.started_at) AS calculated_at
        FROM AIVA_chat_sessions cs
        JOIN AIVA_users u ON u.id = cs.user_id
        LEFT JOIN AIVA_ai_requests ar ON ar.session_id = cs.id
        WHERE cs.account_id = :account_id
        GROUP BY cs.user_id, u.first_name, u.last_name, u.email
        HAVING COUNT(ar.id) > 0
        ORDER BY COUNT(ar.id) DESC, cs.user_id
        """,
        {"account_id": account_id},
    )
    result: list[AgentMetricOut] = []
    for row in rows:
        data = serialize_row(row) or {}
        result.append(
            AgentMetricOut(
                user_id=int(data["user_id"]),
                account_id=int(data["account_id"]),
                agent_first_name=data.get("agent_first_name"),
                agent_last_name=data.get("agent_last_name"),
                agent_email=data.get("agent_email"),
                avg_response_time=float(data["avg_response_time"])
                if data.get("avg_response_time") is not None
                else None,
                ai_usage_count=int(data.get("ai_usage_count") or 0),
                successful_answers=int(data.get("successful_answers") or 0),
                escalation_count=int(data.get("escalation_count") or 0),
                calculated_at=data.get("calculated_at"),
            )
        )
    return result
