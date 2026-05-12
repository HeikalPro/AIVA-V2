"""Routing strategies for multi-provider selection."""

from __future__ import annotations

from typing import Protocol

from llm_service.core.base import BaseLLMProvider
from llm_service.core.models import LLMRequest

# Approximate relative cost table for ordering (arbitrary units).
_COST_TABLE: dict[str, float] = {
    "openai": 1.0,
    "anthropic": 1.1,
    "gemini": 0.8,
    "azure_openai": 1.0,
    "ollama": 0.01,
    "vllm": 0.01,
    "openrouter": 1.0,
    "huggingface": 0.5,
    "local": 0.0,
    "mock": 0.0,
}


class RoutingStrategy(Protocol):
    def order(self, providers: list[BaseLLMProvider], request: LLMRequest) -> list[BaseLLMProvider]: ...


class RoundRobinStrategy:
    def __init__(self) -> None:
        self._idx = 0

    def order(self, providers: list[BaseLLMProvider], request: LLMRequest) -> list[BaseLLMProvider]:
        if not providers:
            return []
        self._idx = (self._idx + 1) % len(providers)
        i = self._idx
        return [providers[i]] + [p for j, p in enumerate(providers) if j != i]


class CostOptimizedStrategy:
    """Prefer lower-cost providers first (heuristic)."""

    def order(self, providers: list[BaseLLMProvider], request: LLMRequest) -> list[BaseLLMProvider]:
        return sorted(providers, key=lambda p: _COST_TABLE.get(p.provider_name, 999.0))


class FallbackStrategy:
    """Use list order as priority (primary → fallback)."""

    def order(self, providers: list[BaseLLMProvider], request: LLMRequest) -> list[BaseLLMProvider]:
        return list(providers)


class LowestLatencyStrategy:
    """Placeholder: would use live latency probes; defaults to original order."""

    def order(self, providers: list[BaseLLMProvider], request: LLMRequest) -> list[BaseLLMProvider]:
        return list(providers)
