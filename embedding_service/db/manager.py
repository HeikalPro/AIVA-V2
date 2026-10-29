from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

import oracledb

if TYPE_CHECKING:
    from embedding_service.config import Settings

_log = logging.getLogger(__name__)


class DatabaseManager:
    """Owns the Oracle connection pool and transactional connection scope."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: oracledb.ConnectionPool | None = None

    def init_pool(self) -> oracledb.ConnectionPool:
        if self._pool is not None:
            return self._pool

        s = self._settings
        kwargs: dict = {
            "user": s.oracle_user,
            "password": s.oracle_password,
            "dsn": s.oracle_dsn,
            "min": s.pool_min,
            "max": s.pool_max,
            "ping_interval": s.oracle_ping_interval,
        }
        if s.oracle_wallet_dir:
            kwargs["config_dir"] = s.oracle_wallet_dir
            kwargs["wallet_location"] = s.oracle_wallet_dir
            if s.oracle_wallet_password:
                kwargs["wallet_password"] = s.oracle_wallet_password

        ms = s.oracle_call_timeout_ms
        if ms is not None and ms > 0:

            def _session_callback(connection: oracledb.Connection, _requested_tag: str) -> None:
                connection.call_timeout = ms

            kwargs["session_callback"] = _session_callback

        self._pool = oracledb.create_pool(**kwargs)
        _log.info("Oracle pool created for DSN %s", s.oracle_dsn)
        return self._pool

    @property
    def pool(self) -> oracledb.ConnectionPool:
        if self._pool is None:
            raise RuntimeError("Pool not initialized; call init_pool() first")
        return self._pool

    @contextmanager
    def connection(self) -> Generator:
        conn = self.pool.acquire()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close_pool(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None
            _log.info("Oracle pool closed")
