"""Response cache interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from llm_service.core.models import LLMResponse


class ResponseCache(ABC):
    @abstractmethod
    async def get(self, key: str) -> LLMResponse | None: ...

    @abstractmethod
    async def set(self, key: str, value: LLMResponse, ttl_seconds: int | None = None) -> None: ...
