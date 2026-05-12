"""Redis-backed cache (optional redis extra)."""

from __future__ import annotations

from llm_service.core.exceptions import CacheError
from llm_service.core.models import LLMResponse

from .base import ResponseCache


class RedisResponseCache(ResponseCache):
    def __init__(self, *, url: str = "redis://localhost:6379/0", prefix: str = "llm:") -> None:
        try:
            import redis.asyncio as redis
        except ImportError as e:  # pragma: no cover
            from llm_service.core.exceptions import ImportExtraError

            raise ImportExtraError("Install redis extra: pip install 'llm-service[redis]'") from e
        self._redis = redis.from_url(url, decode_responses=True)
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> LLMResponse | None:
        try:
            raw = await self._redis.get(self._k(key))
        except Exception as e:  # pragma: no cover
            raise CacheError(str(e)) from e
        if not raw:
            return None
        return LLMResponse.model_validate_json(raw)

    async def set(self, key: str, value: LLMResponse, ttl_seconds: int | None = 300) -> None:
        try:
            await self._redis.set(self._k(key), value.model_dump_json(), ex=ttl_seconds)
        except Exception as e:  # pragma: no cover
            raise CacheError(str(e)) from e
