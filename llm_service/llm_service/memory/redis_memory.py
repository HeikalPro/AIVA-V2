"""Redis-backed conversation memory (optional redis extra)."""

from __future__ import annotations

import json

from llm_service.core.exceptions import ImportExtraError
from llm_service.core.models import Message

from .base import ConversationMemory


class RedisConversationMemory(ConversationMemory):
    def __init__(self, *, url: str, key: str, ttl_seconds: int | None = 3600) -> None:
        try:
            import redis.asyncio as redis
        except ImportError as e:  # pragma: no cover
            raise ImportExtraError("Install redis extra: pip install 'llm-service[redis]'") from e
        self._redis = redis.from_url(url, decode_responses=True)
        self._key = key
        self._ttl = ttl_seconds

    async def append(self, message: Message) -> None:
        raw = await self._redis.get(self._key)
        data = json.loads(raw) if raw else []
        data.append(message.model_dump(mode="json"))
        await self._redis.set(self._key, json.dumps(data), ex=self._ttl)

    async def get(self) -> list[Message]:
        raw = await self._redis.get(self._key)
        if not raw:
            return []
        data = json.loads(raw)
        return [Message.model_validate(m) for m in data]

    async def clear(self) -> None:
        await self._redis.delete(self._key)
