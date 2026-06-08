"""Estimate LLM request cost from token usage and model pricing."""

from __future__ import annotations

from backend.config import Settings, get_settings

# USD per 1M tokens: (input, output)
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.0, 8.0),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-3.5-turbo": (0.5, 1.5),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "gemini-2.0-flash": (0.1, 0.4),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini-1.5-flash": (0.075, 0.3),
}


def _resolve_pricing(model_name: str, settings: Settings) -> tuple[float, float] | None:
    normalized = model_name.lower().strip()
    for key, pricing in _MODEL_PRICING.items():
        if key in normalized:
            return pricing

    input_rate = settings.llm_default_input_usd_per_million_tokens
    output_rate = settings.llm_default_output_usd_per_million_tokens
    if input_rate is not None and output_rate is not None:
        return (input_rate, output_rate)
    return None


def estimate_llm_cost_usd(
    *,
    model_name: str,
    input_tokens: int | None,
    output_tokens: int | None,
    settings: Settings | None = None,
) -> float | None:
    """Return estimated USD cost, or None when pricing or tokens are unavailable."""
    if not input_tokens and not output_tokens:
        return None

    cfg = settings or get_settings()
    pricing = _resolve_pricing(model_name, cfg)
    if pricing is None:
        return None

    input_rate, output_rate = pricing
    cost = (int(input_tokens or 0) / 1_000_000) * input_rate + (int(output_tokens or 0) / 1_000_000) * output_rate
    return round(cost, 6)
