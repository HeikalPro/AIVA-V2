"""Configurable mock provider for tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk, TokenUsage


class MockLLMProvider(BaseLLMProviderConfigurable):
    """Configurable fake for unit/integration tests (separate registry id optional)."""

    def __init__(self, config: Any = None) -> None:
        from llm_service.config.provider_config import MockProviderConfig

        super().__init__(config or MockProviderConfig())
        self._responses: list[str] = list(self._config.extra.get("responses") or ["mock response"])
        self._idx = 0
        self._raise = cast(type[BaseException] | None, self._config.extra.get("raise_on"))

    @property
    def provider_name(self) -> str:
        return "mock"

    def _next_text(self) -> str:
        if self._idx >= len(self._responses):
            self._idx = 0
        text = self._responses[self._idx]
        self._idx += 1
        return text

    async def achat(self, request: LLMRequest) -> LLMResponse:
        if self._raise:
            raise self._raise("mock error")
        content = self._next_text()
        return LLMResponse(
            provider=self.provider_name,
            model=request.model,
            content=content,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        if self._raise:
            raise self._raise("mock stream error")
        text = self._next_text()
        for word in text.split():
            yield StreamChunk(delta=word + " ", correlation_id=request.correlation_id)
        yield StreamChunk(delta="", finish_reason="stop", correlation_id=request.correlation_id)
