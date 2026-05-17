from __future__ import annotations

import pytest
from llm_service.config.provider_config import MockProviderConfig
from llm_service.core.exceptions import RateLimitError, RetryExhaustedError
from llm_service.core.models import LLMRequest, Message
from llm_service.core.types import Role
from llm_service.routing import FallbackStrategy, LLMRouter, RoundRobinStrategy
from llm_service.testing import MockLLMProvider


@pytest.mark.asyncio
async def test_router_fallback_success() -> None:
    good = MockLLMProvider(MockProviderConfig(extra={"responses": ["from-a"]}))
    bad = MockLLMProvider(MockProviderConfig(extra={"raise_on": RateLimitError}))

    router = LLMRouter([bad, good], strategy=FallbackStrategy())
    req = LLMRequest(messages=[Message(role=Role.USER, content="x")], model="m")
    resp = await router.achat(req)
    assert "from-a" in resp.content


@pytest.mark.asyncio
async def test_router_all_fail() -> None:
    class Flaky(MockLLMProvider):
        async def achat(self, request: LLMRequest):  # type: ignore[override]
            raise RateLimitError("nope", provider=self.provider_name)

    p1 = Flaky(MockProviderConfig())
    p2 = Flaky(MockProviderConfig())
    router = LLMRouter([p1, p2], strategy=FallbackStrategy())
    req = LLMRequest(messages=[Message(role=Role.USER, content="x")], model="m")
    with pytest.raises(RetryExhaustedError):
        await router.achat(req)


def test_round_robin_orders() -> None:
    a = MockLLMProvider()
    b = MockLLMProvider()
    strat = RoundRobinStrategy()
    req = LLMRequest(messages=[], model="m")
    o1 = strat.order([a, b], req)
    o2 = strat.order([a, b], req)
    assert o1[0] != o2[0]
