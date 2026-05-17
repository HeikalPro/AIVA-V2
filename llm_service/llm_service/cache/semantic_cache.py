"""Semantic similarity cache (optional embedding stack)."""

from __future__ import annotations

from llm_service.cache.base import ResponseCache
from llm_service.core.exceptions import ImportExtraError
from llm_service.core.models import LLMResponse


class SemanticResponseCache(ResponseCache):
    """
    Placeholder for embedding + vector store backed cache.
    Subclass or replace with a real implementation when using semantic-cache extra.
    """

    async def get(self, key: str) -> LLMResponse | None:
        raise ImportExtraError(
            "SemanticResponseCache is not implemented in core; provide a custom ResponseCache.",
        )

    async def set(self, key: str, value: LLMResponse, ttl_seconds: int | None = None) -> None:
        raise ImportExtraError(
            "SemanticResponseCache is not implemented in core; provide a custom ResponseCache.",
        )
