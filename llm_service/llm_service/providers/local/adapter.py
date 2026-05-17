"""Local model placeholder (extend with transformers, llama.cpp, etc.)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from llm_service.config.provider_config import LocalProviderConfig
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk, TokenUsage
from llm_service.providers.registry import register


@register("local")
class LocalProvider(BaseLLMProviderConfigurable):
    provider_id = "local"

    def __init__(self, config: LocalProviderConfig) -> None:
        super().__init__(config)
        self._config = config

    @property
    def provider_name(self) -> str:
        return "local"

    async def achat(self, request: LLMRequest) -> LLMResponse:
        # Placeholder: wire to local inference stack in production.
        text = (
            f"[local:{self._config.model_path or self._config.default_model}] "
            f"Echo: {request.messages[-1].content!s}"[:4000]
        )
        return LLMResponse(
            provider=self.provider_name,
            model=request.model,
            content=text,
            usage=TokenUsage(),
            finish_reason="stop",
            correlation_id=request.correlation_id,
            metadata={"note": "LocalProvider is a stub; plug in your runtime."},
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        resp = await self.achat(request)
        yield StreamChunk(delta=resp.content, finish_reason="stop", correlation_id=request.correlation_id)
