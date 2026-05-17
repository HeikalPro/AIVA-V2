"""Weighted / health-aware selection helpers."""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterator
from dataclasses import dataclass, field

from llm_service.core.base import BaseLLMProvider


@dataclass
class WeightedRoundRobin:
    weights: dict[str, float] = field(default_factory=dict)

    def pick(self, providers: list[BaseLLMProvider]) -> BaseLLMProvider:
        if not providers:
            raise ValueError("no providers")
        weighted: list[BaseLLMProvider] = []
        for p in providers:
            w = max(self.weights.get(p.provider_name, 1.0), 0.01)
            weighted.extend([p] * int(w * 10))
        return random.choice(weighted)


class HealthAwareBalancer:
    """Skips providers failing an async health predicate (stateful)."""

    def __init__(self) -> None:
        self._bad: set[str] = set()

    def mark_bad(self, name: str) -> None:
        self._bad.add(name)

    def mark_good(self, name: str) -> None:
        self._bad.discard(name)

    def healthy(self, providers: list[BaseLLMProvider]) -> list[BaseLLMProvider]:
        return [p for p in providers if p.provider_name not in self._bad]

    @staticmethod
    def cycle(providers: list[BaseLLMProvider]) -> Iterator[BaseLLMProvider]:
        return iter(itertools.cycle(providers))
