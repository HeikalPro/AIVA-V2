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
    action = str(row.get("action_type") or "")
    entity = str(row.get("entity_type") or "")
    actor = row.get("actor_email") or (f"user #{row['user_id']}" if row.get("user_id") else "system")
    entity_id = row.get("entity_id") or "?"
    return f"{actor} — {action} {entity} #{entity_id}"


def _sign_in_summary(row: dict[str, Any]) -> str:
    email = row.get("user_email") or (f"user #{row['user_id']}" if row.get("user_id") else "unknown")
    event = str(row.get("event_type") or "event")
    ip = row.get("ip_address") or "—"
    return f"{email} — {event} — {ip}"


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
