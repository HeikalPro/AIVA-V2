"""OpenAI REST adapter (OpenAI-compatible /v1/chat/completions)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from llm_service.config.provider_config import OpenAIProviderConfig
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk
from llm_service.providers._base_http import BaseHTTPProvider
from llm_service.providers.registry import register


@register("openai")
class OpenAIProvider(BaseHTTPProvider):
    provider_id = "openai"

    def __init__(self, config: OpenAIProviderConfig) -> None:
        super().__init__(config)
        self._openai_config = config
        if config.organization_id:
            self._client.headers["OpenAI-Organization"] = config.organization_id

    async def achat(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        return await self._openai_chat(request, url=url)

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        async for chunk in self._openai_stream(request, url=url):
            yield chunk
