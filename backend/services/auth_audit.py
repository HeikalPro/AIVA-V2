from __future__ import annotations

import json
from typing import Any

from fastapi import Request

from backend.database import Database


def client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


def user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("user-agent")


async def log_auth_event(
    db: Database,
    *,
    event_type: str,
    user_id: int | None = None,
    request: Request | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO AIVA_auth_audit_logs (user_id, event_type, ip_address, user_agent, metadata)
        VALUES (:user_id, :event_type, :ip_address, :user_agent, :metadata)
        """,
        {
            "user_id": user_id,
            "event_type": event_type,
            "ip_address": client_ip(request),
            "user_agent": user_agent(request),
            "metadata": json.dumps(metadata) if metadata else None,
        },
    )
