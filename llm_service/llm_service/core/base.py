"""Abstract base provider."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from .models import LLMRequest, LLMResponse, StreamChunk


class BaseLLMProvider(ABC):
    """Contract every provider must satisfy. Sync wrappers delegate to async."""

    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @abstractmethod
    async def achat(self, request: LLMRequest) -> LLMResponse: ...

    @abstractmethod
    def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]: ...

    def chat(self, request: LLMRequest) -> LLMResponse:
        return asyncio.run(self.achat(request))

    async def health_check(self) -> bool:
        return True

    async def aclose(self) -> None:
        """Release provider resources (e.g. HTTP pools). Default: no-op."""
        return None

    async def awarmup(self, *, model: str | None = None) -> None:
        """Optional: pre-connect to the API (TLS + pool) to reduce time-to-first-token on the next call."""
        return None

    async def count_tokens(self, request: LLMRequest) -> int:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(provider_name={self.provider_name!r})"


class BaseLLMProviderConfigurable(BaseLLMProvider):
    """Provider with arbitrary config bag (for typing / shared helpers)."""

    def __init__(self, config: Any) -> None:
        self._config = config
