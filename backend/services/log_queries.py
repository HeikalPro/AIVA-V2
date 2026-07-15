from __future__ import annotations

import json
import logging
from typing import Any

from backend.auth.deps import UserContext
from backend.auth.role_constants import ROLE_SUPERVISOR
from backend.database import Database
from backend.utils import serialize_row

_log = logging.getLogger(__name__)

_SUPERVISOR_ACTIVITY_ACTIONS = frozenset(
    {"CREATE", "CREATE_TRAINEE", "ASSIGN", "UNASSIGN", "UPDATE", "UPDATE_ROLE", "DELETE"}
)
_SUPERVISOR_ACTIVITY_ENTITIES = frozenset({"user", "account_user"})


def _parse_json_blob(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _audit_summary(row: dict[str, Any]) -> str:
    action = str(row.get("action_type") or "").replace("_", " ").title()
    entity = str(row.get("entity_type") or "").replace("_", " ")
    actor = row.get("actor_email") or (f"user #{row['user_id']}" if row.get("user_id") else "System")
    entity_id = row.get("entity_id") or "?"
    return f"{actor} performed {action} on {entity} #{entity_id}"


def _sign_in_summary(row: dict[str, Any]) -> str:
    email = row.get("user_email") or (f"user #{row['user_id']}" if row.get("user_id") else "Unknown user")
    event = str(row.get("event_type") or "event").replace("_", " ").title()
    ip = row.get("ip_address") or "—"
    return f"{email} {event.lower()} from {ip}"


def _ai_request_summary(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "?").upper()
    model = row.get("model_name") or "unknown model"
    tokens = (int(row.get("input_tokens") or 0), int(row.get("output_tokens") or 0))
    cost = row.get("total_cost")
    cost_str = f" — ${float(cost):.4f}" if isinstance(cost, (int, float)) else ""
    if status == "FAILED" and row.get("error_message"):
        return f"FAILED ({model}) — {str(row['error_message'])[:120]}"
    return f"{status} — {model} — {tokens[0]}→{tokens[1]} tok{cost_str}"


async def list_ai_requests(
    db: Database,
    *,
    org_id: int | None = None,
    account_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Per-request LLM call log (model, tokens, cost, latency, failures)."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    binds: dict[str, Any] = {"limit": limit, "offset": offset}
    filters = ["1=1"]
    if org_id is not None:
        filters.append("a.organization_id = :org_id")
        binds["org_id"] = org_id
    if account_id is not None:
        filters.append("NVL(ar.account_id, cs.account_id) = :account_id")
        binds["account_id"] = account_id
    if status:
        filters.append("UPPER(TRIM(ar.status)) = :status")
        binds["status"] = status.upper()

    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""
        SELECT ar.id, ar.created_at, ar.session_id, NVL(ar.account_id, cs.account_id) AS account_id,
               ar.model_name, ar.provider, ar.input_tokens, ar.output_tokens,
               ar.response_time_ms, ar.total_cost, ar.status, ar.error_message,
               ar.source, a.name AS account_name, a.organization_id,
               u.email AS user_email
        FROM AIVA_ai_requests ar
        LEFT JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
        LEFT JOIN AIVA_users u ON u.id = cs.user_id
        LEFT JOIN AIVA_accounts a ON a.id = NVL(ar.account_id, cs.account_id)
        WHERE {where_sql}
        ORDER BY ar.id DESC
        OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """,
        binds,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = serialize_row(row) or {}
        data["summary"] = _ai_request_summary(data)
        out.append(data)
    return out


def _ai_metrics_filters(
    *,
    org_id: int | None,
    account_id: int | None,
    status: str | None,
    start: str | None,
    end: str | None,
) -> tuple[list[str], dict[str, Any]]:
    """Shared WHERE clause + binds for the AI-request metric aggregates."""
    binds: dict[str, Any] = {}
    filters = ["1=1"]
    if org_id is not None:
        filters.append("a.organization_id = :org_id")
        binds["org_id"] = org_id
    if account_id is not None:
        filters.append("NVL(ar.account_id, cs.account_id) = :account_id")
        binds["account_id"] = account_id
    if status:
        filters.append("UPPER(TRIM(ar.status)) = :status")
        binds["status"] = status.upper()
    if start:
        filters.append("ar.created_at >= TO_TIMESTAMP(:start_date, 'YYYY-MM-DD')")
        binds["start_date"] = start
    if end:
        # inclusive of the end day
        filters.append("ar.created_at < TO_TIMESTAMP(:end_date, 'YYYY-MM-DD') + 1")
        binds["end_date"] = end
    return filters, binds


async def get_ai_request_metrics(
    db: Database,
    *,
    org_id: int | None = None,
    account_id: int | None = None,
    status: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Aggregate KPIs + per-model breakdown for LLM calls (mirrors the AI-requests tab scope)."""
    filters, binds = _ai_metrics_filters(
        org_id=org_id, account_id=account_id, status=status, start=start, end=end
    )
    where_sql = " AND ".join(filters)
    join_sql = """
        FROM AIVA_ai_requests ar
        LEFT JOIN AIVA_chat_sessions cs ON cs.id = ar.session_id
        LEFT JOIN AIVA_accounts a ON a.id = NVL(ar.account_id, cs.account_id)
    """

    summary_row = await db.fetch_one(
        f"""
        SELECT COUNT(*) AS total_calls,
               AVG(ar.response_time_ms) AS avg_latency,
               MIN(ar.response_time_ms) AS min_latency,
               MAX(ar.response_time_ms) AS max_latency,
               NVL(SUM(ar.input_tokens), 0) + NVL(SUM(ar.output_tokens), 0) AS total_tokens,
               SUM(CASE WHEN UPPER(TRIM(ar.status)) = 'SUCCESS' THEN 1 ELSE 0 END) AS success_count,
               SUM(CASE WHEN UPPER(TRIM(ar.status)) = 'FAILED' THEN 1 ELSE 0 END) AS failed_count
        {join_sql}
        WHERE {where_sql}
        """,
        binds,
    )
    s = serialize_row(summary_row) or {}
    total = int(s.get("total_calls") or 0)
    success = int(s.get("success_count") or 0)
    failed = int(s.get("failed_count") or 0)
    summary = {
        "total_calls": total,
        "avg_latency_ms": float(s["avg_latency"]) if s.get("avg_latency") is not None else None,
        "min_latency_ms": float(s["min_latency"]) if s.get("min_latency") is not None else None,
        "max_latency_ms": float(s["max_latency"]) if s.get("max_latency") is not None else None,
        "total_tokens": int(s.get("total_tokens") or 0),
        "success_count": success,
        "failed_count": failed,
        "success_rate": round(success / total, 4) if total else None,
        "error_rate": round(failed / total, 4) if total else None,
    }

    rows = await db.fetch_all(
        f"""
        SELECT NVL(ar.model_name, 'unknown') AS model_name,
               MAX(ar.provider) AS provider,
               COUNT(*) AS cnt,
               AVG(ar.response_time_ms) AS avg_latency,
               MIN(ar.response_time_ms) AS min_latency,
               MAX(ar.response_time_ms) AS max_latency,
               NVL(SUM(ar.input_tokens), 0) + NVL(SUM(ar.output_tokens), 0) AS total_tokens,
               SUM(CASE WHEN UPPER(TRIM(ar.status)) = 'FAILED' THEN 1 ELSE 0 END) AS failed_count
        {join_sql}
        WHERE {where_sql}
        GROUP BY NVL(ar.model_name, 'unknown')
        ORDER BY COUNT(*) DESC
        """,
        binds,
    )
    by_model: list[dict[str, Any]] = []
    for row in rows:
        d = serialize_row(row) or {}
        cnt = int(d.get("cnt") or 0)
        by_model.append(
            {
                "model_name": str(d.get("model_name") or "unknown"),
                "provider": d.get("provider"),
                "count": cnt,
                "avg_latency_ms": float(d["avg_latency"]) if d.get("avg_latency") is not None else None,
                "min_latency_ms": float(d["min_latency"]) if d.get("min_latency") is not None else None,
                "max_latency_ms": float(d["max_latency"]) if d.get("max_latency") is not None else None,
                "total_tokens": int(d.get("total_tokens") or 0),
                "error_rate": round(int(d.get("failed_count") or 0) / cnt, 4) if cnt else None,
            }
        )

    return {"summary": summary, "by_model": by_model}


def _supervisor_activity_visible(row: dict[str, Any], account_ids: set[int]) -> bool:
    if str(row.get("action_type")) not in _SUPERVISOR_ACTIVITY_ACTIONS:
        return False
    if str(row.get("entity_type")) not in _SUPERVISOR_ACTIVITY_ENTITIES:
        return False
    if not account_ids:
        return True
    payload = _parse_json_blob(row.get("new_value"))
    account_id = payload.get("account_id")
    if account_id is None:
        return str(row.get("action_type")) in {"CREATE_TRAINEE", "CREATE"}
    try:
        return int(account_id) in account_ids
    except (TypeError, ValueError):
        return False


async def list_audit_logs(
    db: Database,
    user: UserContext,
    *,
    limit: int = 100,
    offset: int = 0,
    action_type: str | None = None,
    entity_type: str | None = None,
    account_id: int | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    binds: dict[str, Any] = {"offset": offset}
    filters = ["1=1"]

    if not user.is_super_admin:
        filters.append(
            """
            (
              u.organization_id = :org_id
              OR (
                al.entity_type = 'user'
                AND EXISTS (
                  SELECT 1 FROM AIVA_users tu
                  WHERE TO_CHAR(tu.id) = al.entity_id AND tu.organization_id = :org_id
                )
              )
            )
            """
        )
        binds["org_id"] = user.organization_id

    if action_type:
        filters.append("al.action_type = :action_type")
        binds["action_type"] = action_type.upper()

    if entity_type:
        filters.append("al.entity_type = :entity_type")
        binds["entity_type"] = entity_type.lower()

    if account_id is not None:
        filters.append(
            """
            (
              INSTR(al.new_value, :account_needle) > 0
              OR INSTR(al.old_value, :account_needle) > 0
              OR INSTR(al.new_value, :account_needle_alt) > 0
              OR INSTR(al.old_value, :account_needle_alt) > 0
            )
            """
        )
        binds["account_needle"] = f'"account_id": {account_id}'
        binds["account_needle_alt"] = f'"account_id":{account_id}'

    if user.has_role(ROLE_SUPERVISOR) and not user.is_super_admin and not user.is_org_admin:
        filters.append("al.action_type IN (:sa1, :sa2, :sa3, :sa4, :sa5, :sa6, :sa7)")
        filters.append("al.entity_type IN (:se1, :se2)")
        binds.update(
            {
                "sa1": "CREATE",
                "sa2": "CREATE_TRAINEE",
                "sa3": "ASSIGN",
                "sa4": "UNASSIGN",
                "sa5": "UPDATE",
                "sa6": "UPDATE_ROLE",
                "sa7": "DELETE",
                "se1": "user",
                "se2": "account_user",
            }
        )

    where_sql = " AND ".join(filters)
    fetch_limit = limit * 3 if user.has_role(ROLE_SUPERVISOR) and not user.is_super_admin and not user.is_org_admin else limit

    rows = await db.fetch_all(
        f"""
        SELECT al.id, al.created_at, al.user_id, al.entity_type, al.entity_id,
               al.action_type, al.old_value, al.new_value, al.ip_address,
               u.email AS actor_email, u.organization_id AS actor_org_id
        FROM AIVA_audit_logs al
        LEFT JOIN AIVA_users u ON u.id = al.user_id
        WHERE {where_sql}
        ORDER BY al.created_at DESC, al.id DESC
        OFFSET :offset ROWS FETCH NEXT :fetch_limit ROWS ONLY
        """,
        {**binds, "fetch_limit": fetch_limit},
    )

    out: list[dict[str, Any]] = []
    for row in rows:
        data = serialize_row(row) or {}
        if user.has_role(ROLE_SUPERVISOR) and not user.is_super_admin and not user.is_org_admin:
            if not _supervisor_activity_visible(data, user.account_ids):
                continue
        data["summary"] = _audit_summary(data)
        out.append(data)
        if len(out) >= limit:
            break
    return out


async def list_sign_in_logs(
    db: Database,
    user: UserContext,
    *,
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    binds: dict[str, Any] = {"limit": limit, "offset": offset}
    filters = ["1=1"]

    if not user.is_super_admin:
        filters.append("u.organization_id = :org_id")
        binds["org_id"] = user.organization_id

    if event_type:
        filters.append("al.event_type = :event_type")
        binds["event_type"] = event_type

    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""
        SELECT al.id, al.created_at, al.user_id, al.event_type, al.ip_address,
               al.user_agent, al.metadata,
               u.email AS user_email
        FROM AIVA_auth_audit_logs al
        LEFT JOIN AIVA_users u ON u.id = al.user_id
        WHERE {where_sql}
        ORDER BY al.created_at DESC, al.id DESC
        OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """,
        binds,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        data = serialize_row(row) or {}
        data["summary"] = _sign_in_summary(data)
        out.append(data)
    return out
