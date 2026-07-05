from __future__ import annotations

from backend.database import Database


async def delete_account_dependencies(db: Database, account_id: int) -> None:
    await db.execute(
        """
        DELETE FROM AIVA_ai_requests
        WHERE session_id IN (
            SELECT id FROM AIVA_chat_sessions WHERE account_id = :account_id
        )
        """,
        {"account_id": account_id},
    )
    await db.execute(
        """
        DELETE FROM AIVA_chat_messages
        WHERE session_id IN (
            SELECT id FROM AIVA_chat_sessions WHERE account_id = :account_id
        )
        """,
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_chat_sessions WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_agent_performance_metrics WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_ingestion_requests WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_prompts WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_account_updates WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "UPDATE AIVA_tickets SET account_id = NULL WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_account_role_nav_permissions WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_user_roles WHERE account_id = :account_id",
        {"account_id": account_id},
    )
    await db.execute(
        "DELETE FROM AIVA_account_users WHERE account_id = :account_id",
        {"account_id": account_id},
    )
