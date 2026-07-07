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


async def ensure_account_schema(db: Database) -> None:
    """Add optional account columns introduced after initial schema deploy."""
    if not await _column_exists(db, "AIVA_ACCOUNTS", "kb_source_base_url"):
        await db.execute("ALTER TABLE AIVA_accounts ADD (kb_source_base_url VARCHAR2(512))")
        _log.info("Added AIVA_accounts.kb_source_base_url")
    if not await _column_exists(db, "AIVA_ACCOUNTS", "widget_features"):
        await db.execute("ALTER TABLE AIVA_accounts ADD (widget_features CLOB)")
        _log.info("Added AIVA_accounts.widget_features")
