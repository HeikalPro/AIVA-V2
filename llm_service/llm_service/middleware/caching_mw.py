"""Exact-match response cache middleware."""

from __future__ import annotations

import hashlib
import json

from llm_service.cache.base import ResponseCache
from llm_service.core.models import LLMResponse

from .base import MiddlewareContext, Next


class CachingMiddleware:
    def __init__(self, cache: ResponseCache) -> None:
        self._cache = cache

    @staticmethod
    def _key(request: MiddlewareContext) -> str:
        payload = {
            "model": request.request.model,
            "messages": [m.model_dump(mode="json") for m in request.request.messages],
            "temperature": request.request.temperature,
            "max_tokens": request.request.max_tokens,
            "tools": request.request.tools,
        }
        raw = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def __call__(self, ctx: MiddlewareContext, call_next: Next) -> LLMResponse:
        if ctx.request.stream:
            return await call_next(ctx)
        key = self._key(ctx)
        cached = await self._cache.get(key)
        if cached is not None:
            ctx.metadata["cache_hit"] = True
            return cached
        response = await call_next(ctx)
        await self._cache.set(key, response)
        ctx.metadata["cache_miss"] = True
        return response
