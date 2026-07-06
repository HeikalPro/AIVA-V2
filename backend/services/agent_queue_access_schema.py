"""Per-agent KB queue access (supervisor-assigned)."""
from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def _table_exists(db: Database, table: str) -> bool:
    row = await db.fetch_one(
        """
        SELECT 1 FROM user_tables WHERE table_name = :table_name
        """,
        {"table_name": table.upper()},
    )
    return row is not None


async def ensure_agent_queue_access_schema(db: Database) -> None:
    if await _table_exists(db, "AIVA_AGENT_QUEUE_ACCESS"):
        return

    await db.execute(
        """
        CREATE TABLE AIVA_agent_queue_access (
            account_id   NUMBER NOT NULL,
            user_id      NUMBER NOT NULL,
            queue_key    VARCHAR2(64) NOT NULL,
            assigned_by  NUMBER,
            assigned_at  TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
            PRIMARY KEY (account_id, user_id, queue_key),
            CONSTRAINT fk_aiva_agent_queue_account
                FOREIGN KEY (account_id) REFERENCES AIVA_accounts(id) ON DELETE CASCADE,
            CONSTRAINT fk_aiva_agent_queue_user
                FOREIGN KEY (user_id) REFERENCES AIVA_users(id) ON DELETE CASCADE
        )
        """
    )
    await db.execute(
        "CREATE INDEX idx_aiva_agent_queue_user ON AIVA_agent_queue_access (account_id, user_id)"
    )
    _log.info("Created AIVA_agent_queue_access table")
