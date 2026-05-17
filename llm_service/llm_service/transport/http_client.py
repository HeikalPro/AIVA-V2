"""Shared httpx async client factory (connection pooling)."""

from __future__ import annotations

import os
from typing import Any

import httpx

# Larger keep-alive pool so repeat calls reuse TLS + connections (major win vs one-shot loops).
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=50)


def _want_http2() -> bool:
    return os.environ.get("LLM_HTTP2", "").lower() in ("1", "true", "yes")


def build_async_client(
    *,
    timeout: float | httpx.Timeout | None = None,
    limits: httpx.Limits | None = None,
    headers: dict[str, str] | None = None,
    http2: bool | None = None,
) -> httpx.AsyncClient:
    t = timeout if isinstance(timeout, httpx.Timeout) else httpx.Timeout(timeout or 60.0)
    use_h2 = _want_http2() if http2 is None else http2
    client_kw: dict[str, Any] = {
        "timeout": t,
        "limits": limits or DEFAULT_LIMITS,
        "headers": headers or {},
    }
    if use_h2:
        client_kw["http2"] = True
    try:
        return httpx.AsyncClient(**client_kw)
    except ImportError:
        client_kw.pop("http2", None)
        return httpx.AsyncClient(**client_kw)
