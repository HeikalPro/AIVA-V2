from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import oracledb

from backend.config import Settings, get_settings
from backend.utils import normalize_db_row

_log = logging.getLogger(__name__)
_sql_log = logging.getLogger("aiva.sql")

# Bind keys whose values must never reach the logs.
_SENSITIVE_BIND_KEYS = ("password", "secret", "token", "hash", "otp", "wallet")


def _compact_sql(sql: str) -> str:
    """Collapse whitespace/newlines so a query fits on one log line."""
    return " ".join(sql.split())


def _bind_summary(params: dict[str, Any] | None) -> str:
    """Render bind variables for logging: redact secrets, truncate long strings,
    and show only a type name for non-primitive binds (e.g. Oracle OUT vars)."""
    if not params:
        return "{}"
    parts: list[str] = []
    for key, value in params.items():
        low = key.lower()
        if any(marker in low for marker in _SENSITIVE_BIND_KEYS):
            parts.append(f"{key}=***")
        elif value is None or isinstance(value, (int, float, bool)):
            parts.append(f"{key}={value}")
        elif isinstance(value, str):
            shown = value if len(value) <= 80 else value[:79] + "…"
            parts.append(f"{key}={shown!r}")
        else:
            parts.append(f"{key}=<{type(value).__name__}>")
    return "{" + ", ".join(parts) + "}"


class Database:
    """Async Oracle connection pool for the AIVA application schema."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._pool: oracledb.AsyncConnectionPool | None = None

    async def init_pool(self) -> oracledb.AsyncConnectionPool:
        if self._pool is not None:
            return self._pool

        s = self._settings
        kwargs: dict[str, Any] = {
            "user": s.oracle_user,
            "password": s.oracle_password,
            "dsn": s.oracle_dsn,
            "min": s.oracle_pool_min,
            "max": s.oracle_pool_max,
        }
        if s.oracle_wallet_dir:
            kwargs["config_dir"] = s.oracle_wallet_dir
            kwargs["wallet_location"] = s.oracle_wallet_dir
            if s.oracle_wallet_password:
                kwargs["wallet_password"] = s.oracle_wallet_password

        ms = s.oracle_call_timeout_ms
        if ms is not None and ms > 0:

            async def _session_callback(
                connection: oracledb.AsyncConnection,
                _requested_tag: str,
            ) -> None:
                connection.call_timeout = ms

            kwargs["session_callback"] = _session_callback

        self._pool = oracledb.create_pool_async(**kwargs)
        _log.info("Oracle async pool created for DSN %s", s.oracle_dsn)
        return self._pool

    @property
    def pool(self) -> oracledb.AsyncConnectionPool:
        if self._pool is None:
            raise RuntimeError("Pool not initialized; call init_pool() first")
        return self._pool

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[oracledb.AsyncConnection]:
        conn = await self.pool.acquire()
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            await self.pool.release(conn)

    async def close_pool(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            _log.info("Oracle async pool closed")

    async def _timed_execute(
        self,
        cur: oracledb.AsyncCursor,
        sql: str,
        params: dict[str, Any],
        *,
        op: str,
    ) -> None:
        """Run cur.execute with timing; WARN on slow queries, DEBUG on every query."""
        t0 = time.perf_counter()
        try:
            await cur.execute(sql, params)
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            threshold = self._settings.db_slow_query_ms
            if threshold and elapsed_ms >= threshold:
                _sql_log.warning(
                    "Slow SQL %.0fms [%s]: %s | binds=%s",
                    elapsed_ms, op, _compact_sql(sql), _bind_summary(params),
                )
            elif _sql_log.isEnabledFor(logging.DEBUG):
                _sql_log.debug(
                    "SQL %.1fms [%s]: %s | binds=%s",
                    elapsed_ms, op, _compact_sql(sql), _bind_summary(params),
                )

    async def fetch_one(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        conn: oracledb.AsyncConnection | None = None,
    ) -> dict[str, Any] | None:
        if conn is not None:
            cur = conn.cursor()
            await self._timed_execute(cur, sql, params or {}, op="fetch_one")
            row = await cur.fetchone()
            if row is None:
                return None
            cols = [d[0].lower() for d in cur.description]
            return await normalize_db_row(row, cols)

        async with self.connection() as c:
            return await self.fetch_one(sql, params, conn=c)

    async def fetch_all(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        conn: oracledb.AsyncConnection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is not None:
            cur = conn.cursor()
            await self._timed_execute(cur, sql, params or {}, op="fetch_all")
            rows = await cur.fetchall()
            if not rows:
                return []
            cols = [d[0].lower() for d in cur.description]
            return [await normalize_db_row(row, cols) for row in rows]

        async with self.connection() as c:
            return await self.fetch_all(sql, params, conn=c)

    async def execute(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        conn: oracledb.AsyncConnection | None = None,
        return_id: bool = False,
    ) -> int | None:
        if conn is not None:
            cur = conn.cursor()
            if return_id:
                out_id = cur.var(int)
                await self._timed_execute(
                    cur, sql, {**(params or {}), "out_id": out_id}, op="execute"
                )
                return int(out_id.getvalue()[0])
            await self._timed_execute(cur, sql, params or {}, op="execute")
            return None

        async with self.connection() as c:
            return await self.execute(sql, params, conn=c, return_id=return_id)
