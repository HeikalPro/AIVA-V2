"""In-memory sliding window conversation store."""

from __future__ import annotations

import asyncio

from llm_service.core.models import Message

from .base import ConversationMemory


class InMemoryConversationMemory(ConversationMemory):
    def __init__(self, *, max_messages: int = 50) -> None:
        self._max = max_messages
        self._messages: list[Message] = []
        self._lock = asyncio.Lock()

    async def append(self, message: Message) -> None:
        async with self._lock:
            self._messages.append(message)
            if len(self._messages) > self._max:
                self._messages = self._messages[-self._max :]

    async def get(self) -> list[Message]:
        async with self._lock:
            return list(self._messages)

    async def clear(self) -> None:
        async with self._lock:
            self._messages.clear()
