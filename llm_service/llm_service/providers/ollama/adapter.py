"""Ollama /api/chat adapter."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_service.config.provider_config import OllamaProviderConfig
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.exceptions import ProviderError
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk, TokenUsage
from llm_service.providers._base_http import message_to_openai_dict
from llm_service.providers.registry import register
from llm_service.transport.http_client import build_async_client
from llm_service.transport.retry import run_with_retry


@register("ollama")
class OllamaProvider(BaseLLMProviderConfigurable):
    provider_id = "ollama"

    def __init__(self, config: OllamaProviderConfig) -> None:
        super().__init__(config)
        headers = dict(config.extra_headers)
        if config.api_key:
            headers.setdefault("Authorization", f"Bearer {config.api_key.get_secret_value()}")
        self._client = build_async_client(timeout=config.timeout, headers=headers)

    @property
    def provider_name(self) -> str:
        return "ollama"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def achat(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        url = f"{self._config.base_url.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message_to_openai_dict(m) for m in request.messages],
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        if request.max_tokens is not None:
            payload["options"]["num_predict"] = request.max_tokens

        async def _call() -> httpx.Response:
            return await self._client.post(url, json=payload)

        resp = await run_with_retry(_call, max_attempts=self._config.max_retries)
        if resp.status_code >= 400:
            raise ProviderError(resp.text[:2000], provider=self.provider_name, status_code=resp.status_code)
        data = resp.json()
        msg = data.get("message") or {}
        content = msg.get("content") or ""
        eval_ct = data.get("eval_count")
        prompt_eval = data.get("prompt_eval_count")
        tok = None
        if prompt_eval is not None or eval_ct is not None:
            pt = int(prompt_eval or 0)
            ct = int(eval_ct or 0)
            tok = TokenUsage(prompt_tokens=pt, completion_tokens=ct, total_tokens=pt + ct)
        return LLMResponse(
            provider=self.provider_name,
            model=request.model,
            content=content,
            usage=tok,
            finish_reason=data.get("done_reason"),
            latency_ms=(time.monotonic() - t0) * 1000,
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._config.base_url.rstrip('/')}/api/chat"
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [message_to_openai_dict(m) for m in request.messages],
            "stream": True,
            "options": {"temperature": request.temperature},
        }
        async with self._client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                raise ProviderError(body, provider=self.provider_name, status_code=resp.status_code)
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = data.get("message") or {}
                delta = msg.get("content") or ""
                done = data.get("done")
                fr = "stop" if done else None
                yield StreamChunk(delta=delta, finish_reason=fr, correlation_id=request.correlation_id)
