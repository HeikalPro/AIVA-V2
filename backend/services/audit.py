from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

from backend.database import Database

_log = logging.getLogger(__name__)


def _audit_json(value: Any) -> str:
    def _default(obj: Any) -> str:
        if isinstance(obj, (date, datetime)):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    return json.dumps(value, default=_default)


async def write_audit_log(
    db: Database,
    *,
    user_id: int | None,
    entity_type: str,
    entity_id: str | int,
    action_type: str,
    old_value: Any = None,
    new_value: Any = None,
    ip_address: str | None = None,
) -> None:
    try:
        await db.execute(
            """
            INSERT INTO AIVA_audit_logs (
                user_id, entity_type, entity_id, action_type,
                old_value, new_value, ip_address
            ) VALUES (
                :user_id, :entity_type, :entity_id, :action_type,
                :old_value, :new_value, :ip_address
            )
            """,
            {
                "user_id": user_id,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "action_type": action_type,
                "old_value": _audit_json(old_value) if old_value is not None else None,
                "new_value": _audit_json(new_value) if new_value is not None else None,
                "ip_address": ip_address,
            },
        )
    except Exception:
        _log.exception(
            "Failed to write audit log: %s %s #%s",
            action_type,
            entity_type,
            entity_id,
        )
        raise
