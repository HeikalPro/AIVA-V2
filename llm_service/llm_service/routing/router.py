"""Multi-provider router with fallback semantics."""

from __future__ import annotations

from collections.abc import AsyncIterator

from llm_service.core.base import BaseLLMProvider
from llm_service.core.exceptions import (
    CircuitOpenError,
    RateLimitError,
    RetryExhaustedError,
    TimeoutError,
)
from llm_service.core.models import LLMRequest, LLMResponse, StreamChunk
from llm_service.routing.strategies import FallbackStrategy, RoutingStrategy


class LLMRouter(BaseLLMProvider):
    """
    Wraps multiple providers; uses routing strategy ordering and retries fallbacks.
    """

    def __init__(
        self,
        providers: list[BaseLLMProvider],
        strategy: RoutingStrategy | None = None,
        *,
        name: str = "router",
    ) -> None:
        if not providers:
            raise ValueError("LLMRouter requires at least one provider")
        self._providers = providers
        self._strategy = strategy or FallbackStrategy()
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name

    async def achat(self, request: LLMRequest) -> LLMResponse:
        ordered = self._strategy.order(self._providers, request)
        last_error: BaseException | None = None
        for provider in ordered:
            try:
                resp = await provider.achat(request)
                resp.metadata.setdefault("routed_via", provider.provider_name)
                return resp
            except (RateLimitError, CircuitOpenError, TimeoutError) as e:
                last_error = e
                continue
        raise RetryExhaustedError(
            f"All providers failed: {last_error!s}",
            provider=self.provider_name,
        )

    async def awarmup(self, *, model: str | None = None) -> None:
        if self._providers:
            await self._providers[0].awarmup(model=model)

    async def aclose(self) -> None:
        for p in self._providers:
            await p.aclose()

    async def astream(self, request: LLMRequest) -> AsyncIterator[StreamChunk]:
        ordered = self._strategy.order(self._providers, request)
        last_error: BaseException | None = None
        for provider in ordered:
            try:
                async for chunk in provider.astream(request):
                    yield chunk
                return
            except (RateLimitError, CircuitOpenError, TimeoutError) as e:
                last_error = e
                continue
        raise RetryExhaustedError(
            f"All providers failed: {last_error!s}",
            provider=self.provider_name,
        )
