"""Operational health probes for the System Health dashboard.

Every probe is defensive: a failure in one component returns a "down"/None
result rather than raising, so the overall snapshot always renders.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from backend.database import Database

_log = logging.getLogger(__name__)


async def db_health(db: Database) -> dict[str, Any]:
    """Ping the Oracle pool and measure round-trip latency."""
    start = time.perf_counter()
    try:
        await db.fetch_one("SELECT 1 AS ok FROM dual")
        return {"status": "up", "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
    except Exception as exc:
        _log.warning("DB health check failed", exc_info=True)
        return {"status": "down", "latency_ms": None, "detail": str(exc)[:200]}


async def redis_snapshot(redis_url: str | None, queue_name: str) -> dict[str, Any]:
    """Ping Redis and read the job-queue depth over a single connection."""
    base = {"queue_name": queue_name, "queue_depth": None}
    if not redis_url:
        return {"status": "not_configured", "latency_ms": None, **base}
    try:
        import redis.asyncio as aioredis
    except Exception:
        return {"status": "unknown", "latency_ms": None, "detail": "redis library unavailable", **base}

    client = aioredis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)
    start = time.perf_counter()
    try:
        await client.ping()
        latency = round((time.perf_counter() - start) * 1000, 1)
        depth: int | None
        try:
            depth = int(await client.llen(queue_name))
        except Exception:
            depth = None
        return {"status": "up", "latency_ms": latency, "queue_name": queue_name, "queue_depth": depth}
    except Exception as exc:
        return {"status": "down", "latency_ms": None, "detail": str(exc)[:200], **base}
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def system_resources() -> dict[str, Any]:
    """CPU and memory usage via psutil. Blocks ~0.1s for the CPU sample, so
    call this off the event loop (asyncio.to_thread).

    In a container these reflect the container's cgroup view where supported,
    otherwise the host.
    """
    try:
        import psutil
    except Exception:
        return {
            "cpu_percent": None,
            "memory_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "detail": "psutil not installed",
        }
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": round(float(cpu), 1),
            "memory_percent": round(float(vm.percent), 1),
            "memory_used_mb": round(vm.used / (1024 * 1024)),
            "memory_total_mb": round(vm.total / (1024 * 1024)),
        }
    except Exception as exc:
        _log.warning("resource probe failed", exc_info=True)
        return {
            "cpu_percent": None,
            "memory_percent": None,
            "memory_used_mb": None,
            "memory_total_mb": None,
            "detail": str(exc)[:200],
        }


async def http_traffic_stats(db: Database, window_minutes: int = 5) -> dict[str, Any]:
    """Latency, error rate, and request rate from the HTTP access log over the
    trailing ``window_minutes``."""
    empty = {
        "window_minutes": window_minutes,
        "request_count": None,
        "error_count": None,
        "error_rate": None,
        "requests_per_minute": None,
        "avg_latency_ms": None,
    }
    try:
        row = (
            await db.fetch_one(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) AS errors,
                       AVG(duration_ms) AS avg_ms
                FROM AIVA_http_request_logs
                WHERE created_at > SYSTIMESTAMP - NUMTODSINTERVAL(:mins, 'MINUTE')
                """,
                {"mins": window_minutes},
            )
            or {}
        )
        total = int(row.get("total") or 0)
        errors = int(row.get("errors") or 0)
        avg_ms = row.get("avg_ms")
        return {
            "window_minutes": window_minutes,
            "request_count": total,
            "error_count": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "requests_per_minute": round(total / window_minutes, 1) if window_minutes else None,
            "avg_latency_ms": round(float(avg_ms), 1) if avg_ms is not None else None,
        }
    except Exception:
        _log.warning("HTTP traffic stats failed", exc_info=True)
        return empty
