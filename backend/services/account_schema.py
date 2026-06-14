from __future__ import annotations

import logging

from backend.database import Database

_log = logging.getLogger(__name__)


async def ensure_account_schema(db: Database) -> None:
    """Add optional account columns introduced after initial schema deploy."""
    pass
