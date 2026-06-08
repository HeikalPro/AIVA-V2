"""Shared HTTP provider utilities (OpenAI-compatible and generic REST)."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any, cast
from urllib.parse import quote

import httpx

from llm_service.config.provider_config import ProviderConfigBase
from llm_service.core.base import BaseLLMProviderConfigurable
from llm_service.core.exceptions import (
    AuthenticationError,
    ConnectionError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)
from llm_service.core.models import LLMRequest, LLMResponse, Message, StreamChunk, TokenUsage
from llm_service.transport.http_client import build_async_client
from llm_service.transport.retry import run_with_retry


def message_to_openai_dict(m: Message) -> dict[str, Any]:
    if isinstance(m.content, str):
        content: str | list[dict[str, Any]] = m.content
    else:
        content = []
        for part in m.content:
            if part.type == "text" and part.text:
                content.append({"type": "text", "text": part.text})
            elif part.type == "image_url" and part.image_url:
                content.append({"type": "image_url", "image_url": part.image_url})
        if not content:
            content = ""
    d: dict[str, Any] = {"role": m.role.value, "content": content}
    if m.name:
        d["name"] = m.name
    if m.tool_calls:
        d["tool_calls"] = m.tool_calls
    if m.tool_call_id:
        d["tool_call_id"] = m.tool_call_id
    return d


class BaseHTTPProvider(BaseLLMProviderConfigurable):
    """HTTP transport with shared error mapping and OpenAI-style chat."""

    provider_id: str = "http"

    def __init__(self, config: ProviderConfigBase) -> None:
        super().__init__(config)
        self._config = config
        headers = dict(config.extra_headers)
        if config.api_key:
            headers.setdefault("Authorization", f"Bearer {config.api_key.get_secret_value()}")
        self._client = build_async_client(timeout=config.timeout, headers=headers)

    @property
    def provider_name(self) -> str:
        return self.provider_id

    async def awarmup(self, *, model: str | None = None) -> None:
        """GET /v1/models/{model} (small JSON) so TLS and HTTP keep-alive are paid before stream/chat."""
        mid = model or getattr(self._config, "default_model", None) or "gpt-4o-mini"
        base = self._config.base_url.rstrip("/")
        if not base:
            return
        url = f"{base}/models/{quote(mid, safe='')}"
        try:
            r = await self._client.get(url)
            await r.aread()
        except Exception:
            return

    def _map_httpx_error(self, e: Exception) -> Exception:
        if isinstance(e, httpx.TimeoutException):
            return TimeoutError(str(e), provider=self.provider_name)
        if isinstance(e, httpx.ConnectError):
            return ConnectionError(str(e), provider=self.provider_name)
        return e

    def _map_response_error(self, response: httpx.Response) -> ProviderError:
        code = response.status_code
        text = response.text[:2000]
        if code == 401:
            return AuthenticationError(text, provider=self.provider_name, status_code=code)
        if code == 429:
            retry_after = response.headers.get("retry-after")
            return RateLimitError(
                text,
                provider=self.provider_name,
                status_code=code,
                retry_after=float(retry_after) if retry_after else None,
            )
        if code in {400, 404, 422}:
            return InvalidRequestError(text, provider=self.provider_name, status_code=code)
        return ProviderError(text, provider=self.provider_name, status_code=code)

    async def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:

            async def _call() -> httpx.Response:
                return await self._client.post(url, json=payload)

            resp = await run_with_retry(_call, max_attempts=self._config.max_retries)
        except Exception as e:
            raise self._map_httpx_error(e) from e
        if resp.status_code >= 400:
            raise self._map_response_error(resp)
        return cast(dict[str, Any], resp.json())

    async def _openai_chat(
        self,
        request: LLMRequest,
        *,
        url: str,
        extra: dict[str, Any] | None = None,
    ) -> LLMResponse:
        t0 = time.monotonic()
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [message_to_openai_dict(m) for m in request.messages],
            "temperature": request.temperature,
            "stream": False,
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools
        if extra:
            body.update(extra)
        if request.extra:
            body.update(request.extra)
        data = await self._post_json(url, body)
        choice0 = data["choices"][0]
        msg = choice0.get("message") or {}
        content = msg.get("content") or ""
        usage = data.get("usage") or {}
        tok = None
        if usage:
            tok = TokenUsage(
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                total_tokens=int(usage.get("total_tokens") or 0),
            )
        return LLMResponse(
            provider=self.provider_name,
            model=data.get("model", request.model),
            content=content if isinstance(content, str) else json.dumps(content),
            usage=tok,
            finish_reason=choice0.get("finish_reason"),
            latency_ms=(time.monotonic() - t0) * 1000,
            correlation_id=request.correlation_id,
            tool_calls=msg.get("tool_calls"),
        )

    async def _openai_stream(
        self,
        request: LLMRequest,
        *,
        url: str,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [message_to_openai_dict(m) for m in request.messages],
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools
        if extra:
            body.update(extra)
        if request.extra:
            body.update(request.extra)
        try:
            async with self._client.stream("POST", url, json=body) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", errors="replace")[:2000]
                    raise ProviderError(
                        text,
                        provider=self.provider_name,
                        status_code=resp.status_code,
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    usage_raw = chunk.get("usage")
                    usage = None
                    if usage_raw:
                        usage = TokenUsage(
                            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                            total_tokens=int(usage_raw.get("total_tokens") or 0),
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        if usage:
                            yield StreamChunk(delta="", usage=usage, correlation_id=request.correlation_id)
                        continue
                    delta = choices[0].get("delta") or {}
                    text_delta = delta.get("content") or ""
                    fr = choices[0].get("finish_reason")
                    yield StreamChunk(
                        delta=text_delta,
                        finish_reason=fr,
                        usage=usage,
                        correlation_id=request.correlation_id,
                    )
        except ProviderError:
            raise
        except httpx.TimeoutException as e:
            raise TimeoutError(str(e), provider=self.provider_name) from e
        except httpx.HTTPError as e:
            raise ConnectionError(str(e), provider=self.provider_name) from e

    async def aclose(self) -> None:
        await self._client.aclose()
