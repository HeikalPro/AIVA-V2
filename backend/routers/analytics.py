import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.auth.deps import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
    UserContext,
    require_account_access,
    require_roles_or_nav_permission,
)
from backend.dependencies import DbDep
from backend.exceptions import NotFoundError
from backend.schemas.analytics import AgentMetricOut, AiTimeseriesPoint, DashboardStats
from backend.utils import serialize_row

router = APIRouter(prefix="/analytics", tags=["analytics"])

_log = logging.getLogger(__name__)


@router.get("/dashboard", response_model=DashboardStats)
async def dashboard_stats(
    user: Annotated[
        UserContext,
        Depends(
            require_roles_or_nav_permission(
                "dashboard",
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
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


@router.get("/ai-timeseries", response_model=list[AiTimeseriesPoint])
async def ai_timeseries(
    user: Annotated[
        UserContext,
        Depends(
            require_roles_or_nav_permission(
                "dashboard",
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
    ],
    db: DbDep,
    account_id: int = Query(...),
    days: int = Query(default=30, ge=1, le=180),
) -> list[AiTimeseriesPoint]:
    """Daily LLM call volume, average latency, and token usage for an account.

    Falls back to the chat session's start time when a request row predates the
    ai_requests.created_at column, so history isn't lost.
    """
    account = await db.fetch_one("SELECT organization_id FROM AIVA_accounts WHERE id = :id", {"id": account_id})
    if not account:
        raise NotFoundError("Account not found")
    require_account_access(account_id, user, int(account["organization_id"]))

    rows = await db.fetch_all(
        """
        SELECT TO_CHAR(TRUNC(NVL(ar.created_at, cs.started_at)), 'YYYY-MM-DD') AS day,
               COUNT(*) AS calls,
               AVG(ar.response_time_ms) AS avg_latency,
               NVL(SUM(ar.input_tokens), 0) + NVL(SUM(ar.output_tokens), 0) AS total_tokens
        FROM AIVA_ai_requests ar
        LEFT JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
        WHERE NVL(ar.account_id, cs.account_id) = :account_id
          AND NVL(ar.created_at, cs.started_at) >= SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')
          AND NVL(ar.created_at, cs.started_at) IS NOT NULL
        GROUP BY TRUNC(NVL(ar.created_at, cs.started_at))
        ORDER BY TRUNC(NVL(ar.created_at, cs.started_at))
        """,
        {"account_id": account_id, "days": days},
    )
    result: list[AiTimeseriesPoint] = []
    for row in rows:
        data = serialize_row(row) or {}
        result.append(
            AiTimeseriesPoint(
                day=str(data.get("day")),
                calls=int(data.get("calls") or 0),
                avg_latency_ms=float(data["avg_latency"]) if data.get("avg_latency") is not None else None,
                total_tokens=int(data.get("total_tokens") or 0),
            )
        )
    return result


@router.get("/agents", response_model=list[AgentMetricOut])
async def agent_metrics(
    user: Annotated[
        UserContext,
        Depends(
            require_roles_or_nav_permission(
                "dashboard",
                ROLE_SUPER_ADMIN,
                ROLE_ORG_ADMIN,
                ROLE_ACCOUNT_MANAGER,
                ROLE_SUPERVISOR,
                ROLE_DEVELOPER,
            )
        ),
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
            SUM(CASE WHEN UPPER(TRIM(ar.status)) = 'FAILED' THEN 1 ELSE 0 END) AS failed_answers,
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

    # Distinct failure reasons per agent (most frequent first), for the "why failed"
    # view. Best-effort: a failure here must never break the agent table.
    reasons_by_user: dict[int, str] = {}
    try:
        reason_rows = await db.fetch_all(
            """
            SELECT user_id,
                   LISTAGG(reason, ' | ' ON OVERFLOW TRUNCATE) WITHIN GROUP (ORDER BY cnt DESC) AS failure_reasons
            FROM (
                SELECT cs.user_id AS user_id,
                       SUBSTR(ar.error_message, 1, 150) AS reason,
                       COUNT(*) AS cnt
                FROM AIVA_chat_sessions cs
                JOIN AIVA_ai_requests ar ON ar.session_id = cs.id
                WHERE cs.account_id = :account_id
                  AND UPPER(TRIM(ar.status)) = 'FAILED'
                  AND ar.error_message IS NOT NULL
                GROUP BY cs.user_id, SUBSTR(ar.error_message, 1, 150)
            )
            GROUP BY user_id
            """,
            {"account_id": account_id},
        )
        for rr in reason_rows:
            d = serialize_row(rr) or {}
            if d.get("user_id") is not None and d.get("failure_reasons"):
                reasons_by_user[int(d["user_id"])] = str(d["failure_reasons"])
    except Exception:
        _log.warning("agent failure-reason aggregation failed", exc_info=True)

    result: list[AgentMetricOut] = []
    for row in rows:
        data = serialize_row(row) or {}
        uid = int(data["user_id"])
        result.append(
            AgentMetricOut(
                user_id=uid,
                account_id=int(data["account_id"]),
                agent_first_name=data.get("agent_first_name"),
                agent_last_name=data.get("agent_last_name"),
                agent_email=data.get("agent_email"),
                avg_response_time=float(data["avg_response_time"])
                if data.get("avg_response_time") is not None
                else None,
                ai_usage_count=int(data.get("ai_usage_count") or 0),
                successful_answers=int(data.get("successful_answers") or 0),
                failed_answers=int(data.get("failed_answers") or 0),
                escalation_count=int(data.get("escalation_count") or 0),
                calculated_at=data.get("calculated_at"),
                failure_reasons=reasons_by_user.get(uid),
            )
        )
    return result
