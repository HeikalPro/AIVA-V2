"""Main SDK entry: LLMClient."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from llm_service.config.settings import LibrarySettings
from llm_service.core.base import BaseLLMProvider
from llm_service.core.exceptions import ConfigurationError
from llm_service.core.models import LLMRequest, LLMResponse, Message, StreamChunk
from llm_service.middleware.base import MiddlewareContext
from llm_service.middleware.chain import MiddlewareChain
from llm_service.middleware.logging_mw import LoggingMiddleware
from llm_service.middleware.metrics_mw import MetricsMiddleware
from llm_service.providers.registry import create_provider


def _coerce_messages(messages: list[dict[str, Any] | Message]) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        if isinstance(m, Message):
            out.append(m)
        else:
            out.append(Message.model_validate(m))
    return out


class LLMClient:
    def __init__(
        self,
        provider: str | BaseLLMProvider,
        model: str | None = None,
        *,
        settings: LibrarySettings | None = None,
        middlewares: list[Any] | None = None,
        config: Any | None = None,
    ) -> None:
        self._settings = settings or LibrarySettings()
        if isinstance(provider, BaseLLMProvider):
            self._provider = provider
        else:
            self._provider = create_provider(provider, self._settings, config=config)
        resolved = model or self._settings.resolved_default_model()
        if not resolved:
            raise ConfigurationError(
                "model is required (pass model= or set LLM_DEFAULT_MODEL / provider default)",
            )
        self._model = resolved
        endpoint = self._call_provider
        self._chain = MiddlewareChain(
            middlewares=middlewares or self._default_middlewares(),
            endpoint=endpoint,
        )
        # Reused for sync chat() so httpx keep-alive works across calls (large latency win vs asyncio.run per call).
        self._sync_loop: asyncio.AbstractEventLoop | None = None

    def _default_middlewares(self) -> list[Any]:
        return [LoggingMiddleware(), MetricsMiddleware()]

    async def _call_provider(self, ctx: MiddlewareContext) -> LLMResponse:
        ctx.metadata.setdefault("provider", self._provider.provider_name)
        return await self._provider.achat(ctx.request)

    async def achat(
        self,
        messages: list[dict[str, Any] | Message],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        stream: bool = False,
        tools: list[dict[str, Any]] | None = None,
        correlation_id: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        req = LLMRequest(
            messages=_coerce_messages(messages),
            model=model or self._model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            tools=tools,
            extra=dict(kwargs),
        )
        if correlation_id:
            req = req.model_copy(update={"correlation_id": correlation_id})
        ctx = MiddlewareContext(request=req)
        return await self._chain.execute(ctx)

    def _ensure_sync_loop(self) -> asyncio.AbstractEventLoop:
        if self._sync_loop is None or self._sync_loop.is_closed():
            self._sync_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._sync_loop)
        return self._sync_loop

    def chat(
        self,
        messages: list[dict[str, Any] | Message],
        **kwargs: Any,
    ) -> LLMResponse:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "LLMClient.chat() cannot be used inside a running event loop; use await client.achat(...).",
            )
        loop = self._ensure_sync_loop()
        return loop.run_until_complete(self.achat(messages, **kwargs))

    def close(self) -> None:
        """Close the sync event loop and provider HTTP pools (call when done, or use context manager)."""
        if self._sync_loop is None or self._sync_loop.is_closed():
            self._sync_loop = None
            return
        try:

            async def _shutdown() -> None:
                await self._provider.aclose()

            self._sync_loop.run_until_complete(_shutdown())
        except Exception:
            pass
        self._sync_loop.close()
        self._sync_loop = None

    def __enter__(self) -> LLMClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def astream(
        self,
        messages: list[dict[str, Any] | Message],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        req = LLMRequest(
            messages=_coerce_messages(messages),
            model=model or self._model,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            tools=tools,
            extra=kwargs,
        )
        async for chunk in self._provider.astream(req):
            yield chunk

    async def awarmup(self, *, model: str | None = None) -> None:
        """Pre-establish TLS + HTTP pool to the provider (lowers time-to-first-token on the next call)."""
        await self._provider.awarmup(model=model or self._model)

    def warm(self, *, model: str | None = None) -> None:
        """Sync wrapper for :meth:`awarmup` (for use with :meth:`chat`, not inside an async event loop)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "LLMClient.warm() cannot be used inside a running event loop; use await client.awarmup().",
            )
        loop = self._ensure_sync_loop()
        loop.run_until_complete(self.awarmup(model=model))

    @property
    def provider(self) -> BaseLLMProvider:
        return self._provider

    @property
    def model(self) -> str:
        return self._model
