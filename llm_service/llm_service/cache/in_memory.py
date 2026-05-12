"""In-memory TTL cache (cachetools optional)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from llm_service.core.models import LLMResponse

from .base import ResponseCache


@dataclass
class _Entry:
    value: LLMResponse
    expires_at: float | None


class InMemoryResponseCache(ResponseCache):
    def __init__(self, *, default_ttl_seconds: int | None = 300) -> None:
        self._default_ttl = default_ttl_seconds
        self._data: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> LLMResponse | None:
        async with self._lock:
            ent = self._data.get(key)
            if ent is None:
                return None
            if ent.expires_at is not None and time.monotonic() > ent.expires_at:
                del self._data[key]
                return None
            return ent.value.model_copy(deep=True)

    async def set(self, key: str, value: LLMResponse, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        expires = time.monotonic() + ttl if ttl is not None else None
        async with self._lock:
            self._data[key] = _Entry(value=value.model_copy(deep=True), expires_at=expires)
