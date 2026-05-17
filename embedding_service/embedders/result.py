from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EmbedBatchResult:
    """Output of one embedding API / DB call for a batch of texts."""

    vectors: list[list[float]]
    """Provider-reported tokens for this batch (e.g. OpenAI `usage.total_tokens`), if any."""
    api_total_tokens: int | None = None
    """Local estimate when `api_total_tokens` is missing (chars heuristic)."""
    estimated_tokens: int = 0
