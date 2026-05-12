from __future__ import annotations

import pytest
from llm_service import LLMClient
from llm_service.cache import InMemoryResponseCache
from llm_service.core.models import LLMRequest, LLMResponse, TokenUsage
from llm_service.middleware import CachingMiddleware, MiddlewareChain, MiddlewareContext


@pytest.mark.asyncio
async def test_middleware_chain_order() -> None:
    called: list[str] = []

    async def mw1(ctx: MiddlewareContext, call_next):
        called.append("1")
        return await call_next(ctx)

    async def mw2(ctx: MiddlewareContext, call_next):
        called.append("2")
        return await call_next(ctx)

    async def endpoint(ctx: MiddlewareContext) -> LLMResponse:
        called.append("e")
        return LLMResponse(provider="t", model="m", content="ok", correlation_id=ctx.request.correlation_id)

    chain = MiddlewareChain([mw1, mw2], endpoint)
    req = LLMRequest(messages=[], model="m")
    resp = await chain.execute(MiddlewareContext(request=req))
    assert resp.content == "ok"
    assert called == ["1", "2", "e"]


@pytest.mark.asyncio
async def test_caching_middleware() -> None:
    cache = InMemoryResponseCache(default_ttl_seconds=60)
    hits = {"n": 0}

    async def endpoint(ctx: MiddlewareContext) -> LLMResponse:
        hits["n"] += 1
        return LLMResponse(
            provider="t",
            model=ctx.request.model,
            content="x",
            usage=TokenUsage(),
            correlation_id=ctx.request.correlation_id,
        )

    from llm_service.core.models import Message
    from llm_service.core.types import Role

    mw = CachingMiddleware(cache)
    chain = MiddlewareChain([mw], endpoint)
    req = LLMRequest(
        messages=[Message(role=Role.USER, content="hi")],
        model="m",
        stream=False,
    )
    ctx1 = MiddlewareContext(request=req)
    await chain.execute(ctx1)
    await chain.execute(MiddlewareContext(request=req))
    assert hits["n"] == 1


def test_llm_client_mock_chat() -> None:
    client = LLMClient(provider="mock", model="mock-model")
    resp = client.chat([{"role": "user", "content": "Hello"}])
    assert "mock" in resp.content.lower() or resp.content == "mock response"
