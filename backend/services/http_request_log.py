from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from backend.database import Database
from backend.utils import serialize_row

_log = logging.getLogger(__name__)

# Skip DB writes for noisy or self-referential routes.
_SKIP_PERSIST_PATHS = frozenset({"/health", "/api/http-logs"})


@dataclass
class RequestActor:
    label: str
    user_id: int | None = None
    user_email: str | None = None
    org_id: int | None = None
    user_roles: str | None = None


async def persist_http_request_log(
    db: Database | None,
    *,
    http_method: str,
    path: str,
    query_string: str | None,
    handler_name: str,
    route_template: str,
    status_code: int,
    duration_ms: float,
    actor: RequestActor,
    client_ip: str | None,
) -> None:
    if db is None or path in _SKIP_PERSIST_PATHS:
        return
    try:
        await db.execute(
            """
            INSERT INTO AIVA_http_request_logs (
                http_method, path, query_string, handler_name, route_template,
                status_code, duration_ms, user_id, user_email, org_id,
                user_roles, actor_label, client_ip
            ) VALUES (
                :http_method, :path, :query_string, :handler_name, :route_template,
                :status_code, :duration_ms, :user_id, :user_email, :org_id,
                :user_roles, :actor_label, :client_ip
            )
            """,
            {
                "http_method": http_method[:10],
                "path": path[:512],
                "query_string": (query_string or "")[:1024] or None,
                "handler_name": handler_name[:128],
                "route_template": route_template[:512],
                "status_code": int(status_code),
                "duration_ms": int(round(duration_ms)),
                "user_id": actor.user_id,
                "user_email": (actor.user_email or "")[:255] or None,
                "org_id": actor.org_id,
                "user_roles": (actor.user_roles or "")[:512] or None,
                "actor_label": actor.label[:512],
                "client_ip": (client_ip or "")[:64] or None,
            },
        )
    except Exception:
        _log.debug("Failed to persist HTTP request log", exc_info=True)


async def list_http_request_logs(
    db: Database,
    *,
    org_id: int | None,
    limit: int = 100,
    offset: int = 0,
    http_method: str | None = None,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    binds: dict[str, Any] = {
        "limit": limit,
        "offset": offset,
        "org_id": org_id,
    }
    filters = ["1=1"]
    if org_id is not None:
        filters.append("org_id = :org_id")
    if http_method:
        filters.append("http_method = :http_method")
        binds["http_method"] = http_method.upper()

    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""
        SELECT id, created_at, http_method, path, query_string, handler_name,
               route_template, status_code, duration_ms, user_id, user_email,
               org_id, user_roles, actor_label, client_ip
        FROM AIVA_http_request_logs
        WHERE {where_sql}
        ORDER BY created_at DESC, id DESC
        OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """,
        binds,
    )
    return [serialize_row(r) or {} for r in rows]
