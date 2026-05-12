"""Token-bucket style async rate limiting (per-process)."""

from __future__ import annotations

import asyncio
import time

from llm_service.core.models import LLMResponse

from .base import MiddlewareContext, Next


class RateLimitMiddleware:
    def __init__(self, *, rate: float = 10.0, per_seconds: float = 1.0) -> None:
        self._rate = rate
        self._per = per_seconds
        self._tokens = rate
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def _acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._updated
            self._updated = now
            self._tokens = min(self._rate, self._tokens + elapsed * (self._rate / self._per))
            if self._tokens < 1:
                wait = (1 - self._tokens) * (self._per / self._rate)
                await asyncio.sleep(max(wait, 0))
                self._tokens = 0
            else:
                self._tokens -= 1

    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        await self._acquire()
        return await call_next(ctx)
