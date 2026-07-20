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


def _stats_filters(
    *,
    org_id: int | None,
    start: str | None,
    end: str | None,
) -> tuple[str, dict[str, Any]]:
    """Shared WHERE clause + binds for the request-stat aggregates."""
    binds: dict[str, Any] = {}
    filters = ["1=1"]
    if org_id is not None:
        filters.append("org_id = :org_id")
        binds["org_id"] = org_id
    if start:
        filters.append("created_at >= TO_TIMESTAMP(:start_date, 'YYYY-MM-DD')")
        binds["start_date"] = start
    if end:
        # inclusive of the end day
        filters.append("created_at < TO_TIMESTAMP(:end_date, 'YYYY-MM-DD') + 1")
        binds["end_date"] = end
    return " AND ".join(filters), binds


async def get_http_request_stats(
    db: Database,
    *,
    org_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Aggregate KPIs + per-endpoint and per-user request counts."""
    where_sql, binds = _stats_filters(org_id=org_id, start=start, end=end)

    summary_row = await db.fetch_one(
        f"""
        SELECT COUNT(*) AS total_requests,
               COUNT(DISTINCT user_id) AS unique_users,
               COUNT(DISTINCT http_method || ' ' || NVL(route_template, path)) AS unique_endpoints,
               AVG(duration_ms) AS avg_duration,
               MAX(duration_ms) AS max_duration,
               SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count
        FROM AIVA_http_request_logs
        WHERE {where_sql}
        """,
        binds,
    )
    s = serialize_row(summary_row) or {}
    total = int(s.get("total_requests") or 0)
    errors = int(s.get("error_count") or 0)
    summary = {
        "total_requests": total,
        "unique_users": int(s.get("unique_users") or 0),
        "unique_endpoints": int(s.get("unique_endpoints") or 0),
        "avg_duration_ms": float(s["avg_duration"]) if s.get("avg_duration") is not None else None,
        "max_duration_ms": int(s["max_duration"]) if s.get("max_duration") is not None else None,
        "error_count": errors,
        "error_rate": round(errors / total, 4) if total else None,
    }

    endpoint_rows = await db.fetch_all(
        f"""
        SELECT http_method,
               NVL(route_template, path) AS endpoint,
               MAX(handler_name) AS handler_name,
               COUNT(*) AS cnt,
               COUNT(DISTINCT user_id) AS unique_users,
               AVG(duration_ms) AS avg_duration,
               MAX(duration_ms) AS max_duration,
               SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
               MAX(created_at) AS last_called_at
        FROM AIVA_http_request_logs
        WHERE {where_sql}
        GROUP BY http_method, NVL(route_template, path)
        ORDER BY COUNT(*) DESC
        FETCH FIRST 300 ROWS ONLY
        """,
        binds,
    )
    by_endpoint: list[dict[str, Any]] = []
    for row in endpoint_rows:
        d = serialize_row(row) or {}
        cnt = int(d.get("cnt") or 0)
        err = int(d.get("error_count") or 0)
        by_endpoint.append(
            {
                "http_method": str(d.get("http_method") or "?"),
                "endpoint": str(d.get("endpoint") or "?"),
                "handler_name": d.get("handler_name"),
                "count": cnt,
                "unique_users": int(d.get("unique_users") or 0),
                "avg_duration_ms": float(d["avg_duration"]) if d.get("avg_duration") is not None else None,
                "max_duration_ms": int(d["max_duration"]) if d.get("max_duration") is not None else None,
                "error_count": err,
                "error_rate": round(err / cnt, 4) if cnt else None,
                "last_called_at": d.get("last_called_at"),
            }
        )

    user_rows = await db.fetch_all(
        f"""
        SELECT NVL(user_email, actor_label) AS actor,
               MAX(user_id) AS user_id,
               COUNT(*) AS cnt,
               COUNT(DISTINCT http_method || ' ' || NVL(route_template, path)) AS unique_endpoints,
               SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS error_count,
               MAX(created_at) AS last_seen_at
        FROM AIVA_http_request_logs
        WHERE {where_sql}
        GROUP BY NVL(user_email, actor_label)
        ORDER BY COUNT(*) DESC
        FETCH FIRST 100 ROWS ONLY
        """,
        binds,
    )
    by_user: list[dict[str, Any]] = []
    for row in user_rows:
        d = serialize_row(row) or {}
        by_user.append(
            {
                "actor": str(d.get("actor") or "anonymous"),
                "user_id": d.get("user_id"),
                "count": int(d.get("cnt") or 0),
                "unique_endpoints": int(d.get("unique_endpoints") or 0),
                "error_count": int(d.get("error_count") or 0),
                "last_seen_at": d.get("last_seen_at"),
            }
        )

    return {"summary": summary, "by_endpoint": by_endpoint, "by_user": by_user}


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
    }
    filters = ["1=1"]
    if org_id is not None:
        filters.append("org_id = :org_id")
        binds["org_id"] = org_id
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
