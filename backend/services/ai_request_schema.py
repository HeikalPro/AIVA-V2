from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def _table_exists(db: Database, table: str) -> bool:
    row = await db.fetch_one(
        "SELECT 1 FROM user_tables WHERE table_name = :table_name",
        {"table_name": table.upper()},
    )
    return row is not None


async def _column_exists(db: Database, table: str, column: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT 1 FROM user_tab_cols
        WHERE table_name = :table_name AND column_name = :column_name
        """,
        {"table_name": table.upper(), "column_name": column.upper()},
    )
    return row is not None


async def ensure_ai_request_schema(db: Database) -> None:
    """Add error_message + account_id to AIVA_ai_requests for failure diagnostics.

    AIVA_ai_requests is a pre-existing table; only add missing columns.
    """
    if not await _table_exists(db, "AIVA_AI_REQUESTS"):
        return

    columns = [
        ("error_message", "VARCHAR2(2000)"),
        ("account_id", "NUMBER"),
        ("source", "VARCHAR2(16)"),
    ]
    for col_name, col_type in columns:
        if not await _column_exists(db, "AIVA_AI_REQUESTS", col_name):
            await db.execute(f"ALTER TABLE AIVA_ai_requests ADD ({col_name} {col_type})")
            _log.info("Added AIVA_ai_requests.%s", col_name)
