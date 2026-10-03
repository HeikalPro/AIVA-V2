"""SQL for ``AIVA_health_checks`` (latest state per component) and ``AIVA_health_check_events``.

Timestamps are naive UTC. A status change (including the very first result of a
component) appends one event row; repeated results with the same status do not.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from backend.database import Database
from backend.doc_intel.constants import MAX_ACTION_BYTES, MAX_REASON_BYTES, T_HEALTH_CHECKS, T_HEALTH_EVENTS
from backend.doc_intel.queue_config import to_plain
from backend.doc_intel.textutil import truncate_utf8

if TYPE_CHECKING:
    from backend.doc_intel.health import CheckResult

_MERGE_SQL = f"""
    MERGE INTO {T_HEALTH_CHECKS} h
    USING (SELECT :component_key AS component_key FROM dual) s
    ON (h.component_key = s.component_key)
    WHEN MATCHED THEN UPDATE SET
        h.label = :label,
        h.status = :status,
        h.reason = :reason,
        h.suggested_action = :action,
        h.latency_ms = :latency,
        h.checked_at = :checked_at,
        h.last_success_at = CASE WHEN :status = 'HEALTHY' THEN :checked_at ELSE h.last_success_at END,
        h.last_failure_at = CASE WHEN :status = 'FAILED' THEN :checked_at ELSE h.last_failure_at END,
        h.consecutive_failures = CASE WHEN :status = 'FAILED' THEN h.consecutive_failures + 1 ELSE 0 END
    WHEN NOT MATCHED THEN INSERT (
        component_key, label, status, reason, suggested_action, latency_ms, checked_at,
        last_success_at, last_failure_at, consecutive_failures
    ) VALUES (
        :component_key, :label, :status, :reason, :action, :latency, :checked_at,
        CASE WHEN :status = 'HEALTHY' THEN :checked_at END,
        CASE WHEN :status = 'FAILED' THEN :checked_at END,
        CASE WHEN :status = 'FAILED' THEN 1 ELSE 0 END
    )
"""
# details_json is written by its own UPDATE: python-oracledb binds a string longer than 32 KB
# as a LONG, which a MERGE rejects (ORA-03146) but a plain UPDATE of a CLOB column accepts.
_DETAILS_SQL = f"UPDATE {T_HEALTH_CHECKS} SET details_json = :details WHERE component_key = :component_key"


class HealthRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load_all(self) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            f"""
            SELECT component_key, label, status, reason, suggested_action, details_json, latency_ms,
                   checked_at, last_success_at, last_failure_at, consecutive_failures
            FROM {T_HEALTH_CHECKS}
            """
        )

    async def save(self, result: CheckResult, *, label: str, checked_at: datetime) -> bool:
        """Upsert the component's latest state; returns True when its status changed (event written)."""
        reason = truncate_utf8(result.reason, MAX_REASON_BYTES)
        async with self._db.connection() as conn:
            prev = await self._db.fetch_one(
                f"SELECT status FROM {T_HEALTH_CHECKS} WHERE component_key = :component_key FOR UPDATE",
                {"component_key": result.key},
                conn=conn,
            )
            await self._db.execute(
                _MERGE_SQL,
                {
                    "component_key": result.key,
                    "label": label,
                    "status": result.status,
                    "reason": reason,
                    "action": truncate_utf8(result.suggested_action, MAX_ACTION_BYTES),
                    "latency": result.latency_ms,
                    "checked_at": checked_at,
                },
                conn=conn,
            )
            await self._db.execute(
                _DETAILS_SQL,
                {
                    "details": json.dumps(to_plain(result.details or {}), ensure_ascii=False, default=str),
                    "component_key": result.key,
                },
                conn=conn,
            )
            old_status = prev.get("status") if prev else None
            if old_status == result.status:
                return False
            await self._db.execute(
                f"""
                INSERT INTO {T_HEALTH_EVENTS} (component_key, old_status, new_status, reason, created_at)
                VALUES (:component_key, :old_status, :new_status, :reason, :created_at)
                """,
                {
                    "component_key": result.key,
                    "old_status": old_status,
                    "new_status": result.status,
                    "reason": reason,
                    "created_at": checked_at,
                },
                conn=conn,
            )
        return True

    async def list_events(self, limit: int) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            f"""
            SELECT id, component_key, old_status, new_status, reason, created_at
            FROM {T_HEALTH_EVENTS}
            ORDER BY created_at DESC, id DESC
            FETCH FIRST :limit ROWS ONLY
            """,
            {"limit": max(1, int(limit))},
        )
