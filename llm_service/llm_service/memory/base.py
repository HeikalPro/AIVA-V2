"""Conversation memory interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from llm_service.core.models import Message


class ConversationMemory(ABC):
    @abstractmethod
    async def append(self, message: Message) -> None: ...

    @abstractmethod
    async def get(self) -> list[Message]: ...

    @abstractmethod
    async def clear(self) -> None: ...
