from __future__ import annotations

from backend.database import Database
from backend.services.account_dependencies import delete_account_dependencies


async def organization_delete_summary(db: Database, org_id: int) -> dict:
    org = await db.fetch_one(
        "SELECT id, name, code FROM AIVA_organizations WHERE id = :id",
        {"id": org_id},
    )
    if not org:
        return {}

    user_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_users WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    account_rows = await db.fetch_all(
        "SELECT id, name FROM AIVA_accounts WHERE organization_id = :org_id ORDER BY name",
        {"org_id": org_id},
    )
    ticket_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_tickets WHERE organization_id = :org_id",
        {"org_id": org_id},
    )

    user_count = int(user_row["cnt"]) if user_row else 0
    account_count = len(account_rows)
    ticket_count = int(ticket_row["cnt"]) if ticket_row else 0

    return {
        "organization_id": int(org["id"]),
        "name": str(org["name"]),
        "code": str(org["code"]),
        "user_count": user_count,
        "account_count": account_count,
        "ticket_count": ticket_count,
        "account_names": [str(r["name"]) for r in account_rows],
    }


async def _clear_user_references_in_org(db: Database, org_id: int) -> None:
    """Remove rows that reference users in this organization (FK-safe order)."""
    await db.execute(
        """
        DELETE FROM AIVA_audit_logs
        WHERE user_id IN (SELECT id FROM AIVA_users WHERE organization_id = :org_id)
        """,
        {"org_id": org_id},
    )
    await db.execute(
        """
        DELETE FROM AIVA_agent_performance_metrics
        WHERE user_id IN (SELECT id FROM AIVA_users WHERE organization_id = :org_id)
        """,
        {"org_id": org_id},
    )
    await db.execute(
        """
        DELETE FROM AIVA_ingestion_requests
        WHERE requested_by IN (SELECT id FROM AIVA_users WHERE organization_id = :org_id)
        """,
        {"org_id": org_id},
    )
    await db.execute(
        """
        UPDATE AIVA_account_users
        SET assigned_by = NULL
        WHERE assigned_by IN (SELECT id FROM AIVA_users WHERE organization_id = :org_id)
        """,
        {"org_id": org_id},
    )


async def delete_user_dependencies(db: Database, user_id: int) -> None:
    await db.execute(
        "DELETE FROM AIVA_audit_logs WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        """
        DELETE FROM AIVA_ai_requests
        WHERE session_id IN (SELECT id FROM AIVA_chat_sessions WHERE user_id = :user_id)
        """,
        {"user_id": user_id},
    )
    await db.execute(
        """
        DELETE FROM AIVA_chat_messages
        WHERE session_id IN (SELECT id FROM AIVA_chat_sessions WHERE user_id = :user_id)
        """,
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_chat_sessions WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_agent_performance_metrics WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_ingestion_requests WHERE requested_by = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_login_history WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_user_roles WHERE user_id = :user_id",
        {"user_id": user_id},
    )
    await db.execute(
        "DELETE FROM AIVA_account_users WHERE user_id = :user_id",
        {"user_id": user_id},
    )


async def delete_organization_cascade(db: Database, org_id: int) -> dict[str, int]:
    accounts = await db.fetch_all(
        "SELECT id FROM AIVA_accounts WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    for row in accounts:
        account_id = int(row["id"])
        await delete_account_dependencies(db, account_id)
        await db.execute("DELETE FROM AIVA_accounts WHERE id = :id", {"id": account_id})

    ticket_row = await db.fetch_one(
        "SELECT COUNT(*) AS cnt FROM AIVA_tickets WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    ticket_count = int(ticket_row["cnt"]) if ticket_row else 0
    await db.execute(
        "DELETE FROM AIVA_tickets WHERE organization_id = :org_id",
        {"org_id": org_id},
    )

    await _clear_user_references_in_org(db, org_id)

    users = await db.fetch_all(
        "SELECT id FROM AIVA_users WHERE organization_id = :org_id",
        {"org_id": org_id},
    )
    for row in users:
        await delete_user_dependencies(db, int(row["id"]))
    user_count = len(users)
    await db.execute(
        "DELETE FROM AIVA_users WHERE organization_id = :org_id",
        {"org_id": org_id},
    )

    await db.execute("DELETE FROM AIVA_organizations WHERE id = :id", {"id": org_id})

    return {
        "accounts_deleted": len(accounts),
        "users_deleted": user_count,
        "tickets_deleted": ticket_count,
    }
