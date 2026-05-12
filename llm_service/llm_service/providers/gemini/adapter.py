"""Google Gemini generateContent adapter (REST)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from llm_service.config.provider_config import GeminiProviderConfig
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.exceptions import ConfigurationError, ProviderError
from llm_service.core.models import LLMRequest, LLMResponse, Message, StreamChunk, TokenUsage
from llm_service.core.types import Role
from llm_service.providers.registry import register
from llm_service.transport.http_client import build_async_client
from llm_service.transport.retry import run_with_retry


def _parts_for_message(m: Message) -> list[dict[str, Any]]:
    if isinstance(m.content, str):
        return [{"text": m.content}]
    parts: list[dict[str, Any]] = []
    for p in m.content:
        if p.type == "text" and p.text:
            parts.append({"text": p.text})
    return parts


@register("gemini")
class GeminiProvider(BaseLLMProviderConfigurable):
    provider_id = "gemini"

    def __init__(self, config: GeminiProviderConfig) -> None:
        super().__init__(config)
        self._config = config
        if not config.api_key:
            raise ConfigurationError("Gemini requires GEMINI_API_KEY (api_key)")
        self._client = build_async_client(timeout=config.timeout, headers=dict(config.extra_headers))

    @property
    def provider_name(self) -> str:
        return "gemini"

    async def aclose(self) -> None:
        await self._client.aclose()

    def _build_payload(self, request: LLMRequest) -> dict[str, Any]:
        contents: list[dict[str, Any]] = []
        for m in request.messages:
            role = "user" if m.role in (Role.USER, Role.SYSTEM) else "model"
            contents.append({"role": role, "parts": _parts_for_message(m)})
        gen_cfg: dict[str, Any] = {"temperature": request.temperature}
        if request.max_tokens:
            gen_cfg["maxOutputTokens"] = request.max_tokens
        body: dict[str, Any] = {"contents": contents, "generationConfig": gen_cfg}
        if request.tools:
            body["tools"] = request.tools
        return body

    async def achat(self, request: LLMRequest) -> LLMResponse:
        t0 = time.monotonic()
        key = self._config.api_key.get_secret_value() if self._config.api_key else ""
        mid = request.model.removeprefix("models/")
        url = f"{self._config.base_url.rstrip('/')}/v1beta/models/{mid}:generateContent?key={key}"
        payload = self._build_payload(request)

        async def _call() -> httpx.Response:
            return await self._client.post(url, json=payload)

        resp = await run_with_retry(_call, max_attempts=self._config.max_retries)
        if resp.status_code >= 400:
            raise ProviderError(resp.text[:2000], provider=self.provider_name, status_code=resp.status_code)
        data = resp.json()
        text = ""
        for cand in data.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                text += part.get("text", "")
        usage_raw = data.get("usageMetadata") or {}
        tok = TokenUsage(
            prompt_tokens=int(usage_raw.get("promptTokenCount") or 0),
            completion_tokens=int(usage_raw.get("candidatesTokenCount") or 0),
            total_tokens=int(usage_raw.get("totalTokenCount") or 0),
        )
        finish = None
        if data.get("candidates"):
            finish = data["candidates"][0].get("finishReason")
        return LLMResponse(
            provider=self.provider_name,
            model=request.model,
            content=text,
            usage=tok,
            finish_reason=str(finish) if finish else None,
            latency_ms=(time.monotonic() - t0) * 1000,
            correlation_id=request.correlation_id,
        )

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        key = self._config.api_key.get_secret_value() if self._config.api_key else ""
        mid = request.model.removeprefix("models/")
        url = f"{self._config.base_url.rstrip('/')}/v1beta/models/{mid}:streamGenerateContent?alt=sse&key={key}"
        payload = self._build_payload(request)
        async with self._client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                raise ProviderError(body, provider=self.provider_name, status_code=resp.status_code)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for cand in data.get("candidates", []):
                    for part in cand.get("content", {}).get("parts", []):
                        if "text" in part:
                            yield StreamChunk(delta=part["text"], correlation_id=request.correlation_id)
