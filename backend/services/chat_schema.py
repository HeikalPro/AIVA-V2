from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def _column_exists(db: Database, table: str, column: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT 1 FROM user_tab_cols
        WHERE table_name = :table_name AND column_name = :column_name
        """,
        {"table_name": table.upper(), "column_name": column.upper()},
    )
    return row is not None


async def ensure_chat_schema(db: Database) -> None:
    """Add agent rating columns on chat messages for widget feedback."""
    columns = [
        ("agent_rating", "VARCHAR2(10)"),
        ("agent_feedback", "CLOB"),
        ("rated_at", "TIMESTAMP"),
        ("rated_by_user_id", "NUMBER"),
        ("kb_sources_json", "CLOB"),
    ]
    for col_name, col_type in columns:
        if not await _column_exists(db, "AIVA_CHAT_MESSAGES", col_name):
            await db.execute(f"ALTER TABLE AIVA_chat_messages ADD ({col_name} {col_type})")
            _log.info("Added AIVA_chat_messages.%s", col_name)
