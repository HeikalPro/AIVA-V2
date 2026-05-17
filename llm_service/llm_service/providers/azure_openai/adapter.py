"""Azure OpenAI (OpenAI-compatible deployment URL)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from urllib.parse import quote

from llm_service.config.provider_config import AzureOpenAIProviderConfig
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk
from llm_service.providers._base_http import BaseHTTPProvider
from llm_service.providers.registry import register, register_alias


@register("azure_openai")
class AzureOpenAIProvider(BaseHTTPProvider):
    provider_id = "azure_openai"

    def __init__(self, config: AzureOpenAIProviderConfig) -> None:
        super().__init__(config)
        self._az = config

    def _chat_url(self, request: LLMRequest) -> str:
        endpoint = self._az.azure_endpoint.rstrip("/") or self._config.base_url.rstrip("/")
        deployment = quote(self._az.deployment_name or request.model, safe="")
        ver = quote(self._az.api_version, safe="")
        return f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={ver}"

    async def achat(self, request: LLMRequest) -> LLMResponse:
        return await self._openai_chat(request, url=self._chat_url(request))

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        async for chunk in self._openai_stream(request, url=self._chat_url(request)):
            yield chunk

    async def awarmup(self, *, model: str | None = None) -> None:
        """GET deployment metadata (small) to warm TLS for Azure host."""
        endpoint = self._az.azure_endpoint.rstrip("/") or self._config.base_url.rstrip("/")
        if not endpoint:
            return
        dep = quote(self._az.deployment_name or model or self._config.default_model, safe="")
        ver = quote(self._az.api_version, safe="")
        url = f"{endpoint}/openai/deployments/{dep}?api-version={ver}"
        try:
            r = await self._client.get(url)
            await r.aread()
        except Exception:
            return


register_alias("azure", "azure_openai")
