"""vLLM OpenAI-compatible server."""

from __future__ import annotations

from collections.abc import AsyncIterator

from llm_service.config.provider_config import VLLMProviderConfig
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk
from llm_service.providers._base_http import BaseHTTPProvider
from llm_service.providers.registry import register


@register("vllm")
class VLLMProvider(BaseHTTPProvider):
    provider_id = "vllm"

    def __init__(self, config: VLLMProviderConfig) -> None:
        super().__init__(config)

    async def achat(self, request: LLMRequest) -> LLMResponse:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        return await self._openai_chat(request, url=url)

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        async for c in self._openai_stream(request, url=url):
            yield c
