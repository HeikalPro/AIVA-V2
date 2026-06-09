from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def ensure_account_schema(db: Database) -> None:
    """Add optional account columns introduced after initial schema deploy."""
    col = await db.fetch_one(
        """
        SELECT 1 FROM user_tab_cols
        WHERE table_name = 'AIVA_ACCOUNTS' AND column_name = 'API_KEY_RENEWAL_DATE'
        """
    )
    if not col:
        await db.execute("ALTER TABLE AIVA_accounts ADD (api_key_renewal_date DATE)")
        _log.info("Added AIVA_accounts.api_key_renewal_date column")
