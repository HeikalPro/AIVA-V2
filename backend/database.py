from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import oracledb

from backend.config import Settings, get_settings
from backend.utils import normalize_db_row

_log = logging.getLogger(__name__)


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
            "ping_interval": s.oracle_ping_interval,
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

    async def fetch_one(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        conn: oracledb.AsyncConnection | None = None,
    ) -> dict[str, Any] | None:
        if conn is not None:
            cur = conn.cursor()
            await cur.execute(sql, params or {})
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
            await cur.execute(sql, params or {})
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
                await cur.execute(sql, {**(params or {}), "out_id": out_id})
                return int(out_id.getvalue()[0])
            await cur.execute(sql, params or {})
            return None

        async with self.connection() as c:
            return await self.execute(sql, params, conn=c, return_id=return_id)
