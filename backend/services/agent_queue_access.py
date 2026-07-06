"""Resolve and manage per-agent queue access for an account."""
from __future__ import annotations

from backend.auth.deps import UserContext
from backend.auth.role_constants import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
)
from backend.database import Database
from backend.exceptions import ForbiddenError
from backend.services.kb_queue_groups import all_queue_keys, list_queue_catalog, validate_active_queues


def user_bypasses_queue_restrictions(user: UserContext) -> bool:
    elevated = {
        ROLE_SUPER_ADMIN,
        ROLE_ORG_ADMIN,
        ROLE_ACCOUNT_MANAGER,
        ROLE_SUPERVISOR,
    }
    return bool(elevated.intersection(user.role_names))


async def list_assigned_queue_keys_by_account(
    db: Database,
    *,
    account_id: int,
) -> dict[int, list[str]]:
    rows = await db.fetch_all(
        """
        SELECT user_id, queue_key
        FROM AIVA_agent_queue_access
        WHERE account_id = :account_id
        ORDER BY user_id, queue_key
        """,
        {"account_id": account_id},
    )
    out: dict[int, list[str]] = {}
    for row in rows:
        uid = int(row["user_id"])
        out.setdefault(uid, []).append(str(row["queue_key"]))
    return out


def build_agent_queue_summaries(
    *,
    user_ids: list[int],
    corpus_config: dict | None,
    assigned_by_user: dict[int, list[str]],
) -> list[dict]:
    catalog = list_queue_catalog(corpus_config)
    catalog_by_key = {item["key"]: item for item in catalog}
    all_keys = [item["key"] for item in catalog]
    summaries: list[dict] = []
    for user_id in user_ids:
        assigned = assigned_by_user.get(user_id, [])
        if assigned:
            keys = [k for k in assigned if k in catalog_by_key]
            restricted = True
        else:
            keys = all_keys
            restricted = False
        summaries.append(
            {
                "user_id": user_id,
                "queues": [catalog_by_key[k] for k in keys],
                "is_restricted": restricted,
            }
        )
    return summaries


async def list_assigned_queue_keys(db: Database, *, account_id: int, user_id: int) -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT queue_key
        FROM AIVA_agent_queue_access
        WHERE account_id = :account_id AND user_id = :user_id
        ORDER BY queue_key
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    return [str(r["queue_key"]) for r in rows]


async def get_allowed_queue_keys(
    db: Database,
    user: UserContext,
    *,
    account_id: int,
    corpus_config: dict | None,
) -> list[str]:
    catalog = all_queue_keys(corpus_config)
    if user_bypasses_queue_restrictions(user):
        return catalog
    assigned = await list_assigned_queue_keys(db, account_id=account_id, user_id=user.id)
    if not assigned:
        return catalog
    allowed = [k for k in assigned if k in catalog]
    return allowed or catalog


async def set_assigned_queue_keys(
    db: Database,
    *,
    account_id: int,
    user_id: int,
    queue_keys: list[str],
    corpus_config: dict | None,
    assigned_by: int,
) -> list[str]:
    normalized = validate_active_queues(
        corpus_config,
        queue_keys,
        allowed_queue_keys=all_queue_keys(corpus_config),
    )
    if normalized is None:
        raise ValueError("At least one queue is required")

    await db.execute(
        """
        DELETE FROM AIVA_agent_queue_access
        WHERE account_id = :account_id AND user_id = :user_id
        """,
        {"account_id": account_id, "user_id": user_id},
    )
    for key in normalized:
        await db.execute(
            """
            INSERT INTO AIVA_agent_queue_access (account_id, user_id, queue_key, assigned_by)
            VALUES (:account_id, :user_id, :queue_key, :assigned_by)
            """,
            {
                "account_id": account_id,
                "user_id": user_id,
                "queue_key": key,
                "assigned_by": assigned_by,
            },
        )
    return normalized


async def require_can_manage_agent_queues(
    db: Database,
    user: UserContext,
    *,
    account_id: int,
    target_user_id: int,
) -> None:
    if user.is_super_admin:
        return
    account = await db.fetch_one(
        "SELECT organization_id FROM AIVA_accounts WHERE id = :id",
        {"id": account_id},
    )
    if not account:
        raise ForbiddenError("Account not found")
    if not user.can_access_account(account_id, int(account["organization_id"])):
        raise ForbiddenError("No access to this account")
    if ROLE_SUPERVISOR in user.role_names or ROLE_ACCOUNT_MANAGER in user.role_names:
        return
    if user.is_org_admin and int(account["organization_id"]) == user.organization_id:
        return
    raise ForbiddenError("Insufficient permissions to manage agent queue access")
