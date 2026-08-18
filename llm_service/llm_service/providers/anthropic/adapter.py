"""Anthropic Messages API adapter."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_service.config.provider_config import AnthropicProviderConfig
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.exceptions import ProviderError
from llm_service.core.models import LLMRequest, LLMResponse, Message, StreamChunk, TokenUsage
from llm_service.core.types import Role
from llm_service.providers.registry import register
from llm_service.transport.http_client import build_async_client
from llm_service.transport.retry import run_with_retry


def _msg_content(m: Message) -> str:
    if isinstance(m.content, str):
        return m.content
    parts = []
    for p in m.content:
        if p.type == "text" and p.text:
            parts.append(p.text)
    return "\n".join(parts)


@register("anthropic")
class AnthropicProvider(BaseLLMProviderConfigurable):
    provider_id = "anthropic"

    def __init__(self, config: AnthropicProviderConfig) -> None:
        super().__init__(config)
        self._config = config
        headers = {
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            **config.extra_headers,
        }
        if config.api_key:
            headers["x-api-key"] = config.api_key.get_secret_value()
        self._client = build_async_client(timeout=config.timeout, headers=headers)

    @property
    def provider_name(self) -> str:
        return "anthropic"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_body(self, request: LLMRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for m in request.messages:
            if m.role == Role.SYSTEM:
                system_parts.append(_msg_content(m))
            else:
                messages.append({"role": m.role.value, "content": _msg_content(m)})
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or 1024,
            "messages": messages,
            "temperature": request.temperature,
        }
        if system_parts:
            body["system"] = "\n".join(system_parts)
        if request.tools:
            body["tools"] = request.tools
        if request.extra:
            body.update(request.extra)
        return body

    async def achat(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        url = f"{self._config.base_url.rstrip('/')}/v1/messages"
        payload = self._build_body(request)

        async def _call() -> httpx.Response:
            return await self._client.post(url, json=payload)

        resp = await run_with_retry(_call, max_attempts=self._config.max_retries)
        if resp.status_code >= 400:
            raise ProviderError(resp.text[:2000], provider=self.provider_name, status_code=resp.status_code)
        data = resp.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        usage = data.get("usage") or {}
        tok = TokenUsage(
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            total_tokens=int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        )
        return LLMResponse(
            provider=self.provider_name,
            model=data.get("model", request.model),
            content=text,
            usage=tok,
            finish_reason=data.get("stop_reason"),
            latency_ms=(time.monotonic() - t0) * 1000,
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        url = f"{self._config.base_url.rstrip('/')}/v1/messages"
        payload = self._build_body(request)
        payload["stream"] = True
        async with self._client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                raise ProviderError(body, provider=self.provider_name, status_code=resp.status_code)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if raw == "[DONE]":
                    break
                try:
                    evt = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if evt.get("type") == "message_start":
                    usage = (evt.get("message") or {}).get("usage") or {}
                    input_tokens = int(usage.get("input_tokens") or 0)
                    if input_tokens:
                        yield StreamChunk(
                            delta="",
                            usage=TokenUsage(
                                prompt_tokens=input_tokens,
                                completion_tokens=0,
                                total_tokens=input_tokens,
                            ),
                            correlation_id=request.correlation_id,
                        )
                if evt.get("type") == "content_block_delta":
                    delta = evt.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        yield StreamChunk(delta=delta.get("text") or "", correlation_id=request.correlation_id)
                if evt.get("type") == "message_delta":
                    usage = evt.get("usage") or {}
                    output_tokens = int(usage.get("output_tokens") or 0)
                    if output_tokens:
                        yield StreamChunk(
                            delta="",
                            usage=TokenUsage(
                                prompt_tokens=0,
                                completion_tokens=output_tokens,
                                total_tokens=output_tokens,
                            ),
                            correlation_id=request.correlation_id,
                        )
                if evt.get("type") == "message_stop":
                    yield StreamChunk(delta="", finish_reason="stop", correlation_id=request.correlation_id)
