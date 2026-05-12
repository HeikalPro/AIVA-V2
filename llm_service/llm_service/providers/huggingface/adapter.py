"""Hugging Face Inference API adapter."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_service.config.provider_config import HuggingFaceProviderConfig
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.exceptions import ProviderError
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk, TokenUsage
from llm_service.providers.registry import register, register_alias
from llm_service.transport.http_client import build_async_client
from llm_service.transport.retry import run_with_retry


@register("huggingface")
class HuggingFaceProvider(BaseLLMProviderConfigurable):
    provider_id = "huggingface"

    def __init__(self, config: HuggingFaceProviderConfig) -> None:
        super().__init__(config)
        headers = dict(config.extra_headers)
        if config.api_key:
            headers.setdefault("Authorization", f"Bearer {config.api_key.get_secret_value()}")
        self._client = build_async_client(timeout=config.timeout, headers=headers)

    @property
    def provider_name(self) -> str:
        return "huggingface"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def achat(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        model = request.model
        url = f"{self._config.base_url.rstrip('/')}/models/{model}"
        # Conversational tasks often use inputs + parameters
        payload: dict[str, Any] = {
            "inputs": request.messages[-1].content
            if isinstance(request.messages[-1].content, str)
            else str(request.messages[-1].content),
            "parameters": {"temperature": request.temperature, "max_new_tokens": request.max_tokens or 256},
        }

        async def _call() -> httpx.Response:
            return await self._client.post(url, json=payload)

        resp = await run_with_retry(_call, max_attempts=self._config.max_retries)
        if resp.status_code >= 400:
            raise ProviderError(resp.text[:2000], provider=self.provider_name, status_code=resp.status_code)
        data = resp.json()
        content: str
        if isinstance(data, list) and data:
            content = str(data[0].get("generated_text", data[0]))
        elif isinstance(data, dict):
            content = str(data.get("generated_text", data))
        else:
            content = str(data)
        return LLMResponse(
            provider=self.provider_name,
            model=model,
            content=content,
            usage=TokenUsage(),
            latency_ms=(time.monotonic() - t0) * 1000,
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        # HF server-sent events vary; fallback non-stream
        resp = await self.achat(request)
        yield StreamChunk(delta=resp.content, finish_reason="stop", correlation_id=request.correlation_id)


register_alias("hf", "huggingface")
