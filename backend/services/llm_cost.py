"""Estimate LLM request cost from token usage and model pricing."""

from __future__ import annotations

from backend.config import Settings, get_settings

# USD per 1M tokens: (input, output)
# NOTE: SovereignEG catalog entries below are placeholders — adjust to your contracted
# rates. When the provider returns a cost in the response usage, that value is used
# instead of these (see estimate_llm_cost_usd's provider_cost_usd argument).
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
    "claude-haiku-4-5": (0.8, 4.0),
    "claude-3-opus": (15.0, 75.0),
    "gemini-2.0-flash": (0.1, 0.4),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini-1.5-flash": (0.075, 0.3),
    # --- SovereignEG catalog (placeholder rates) ---
    "gpt-5.5": (2.0, 8.0),
    "gpt-oss-120b-turbo": (0.3, 1.0),
    "gemini-3.5-flash-2": (0.1, 0.4),
    "gemini-3.5-flash": (0.1, 0.4),
    "gemini-3.1-pro-2": (1.25, 5.0),
    "deepseek-v4-flash": (0.15, 0.6),
    "deepseek-r1-0528": (0.55, 2.2),
    "glm-4.7-flash": (0.1, 0.4),
    "qwen-2-1.5b-instruct": (0.05, 0.2),
    "nemotron-3-nano-30b-a3b-2": (0.1, 0.4),
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


def apply_cost_markup(cost: float, cfg: Settings) -> float:
    """Add the configured markup (e.g. +10%) on top of the raw cost."""
    markup = getattr(cfg, "llm_cost_markup_pct", 0.0) or 0.0
    return round(cost * (1 + markup / 100.0), 6)


# Backwards-compatible alias.
_apply_markup = apply_cost_markup


def estimate_llm_cost_usd(
    *,
    model_name: str,
    input_tokens: int | None,
    output_tokens: int | None,
    provider_cost_usd: float | None = None,
    settings: Settings | None = None,
) -> float | None:
    """Return the reported USD cost (with markup applied), or None when unavailable.

    Prefers a cost reported by the provider in the response (``provider_cost_usd``);
    otherwise estimates from token counts and the local price table. The configured
    ``llm_cost_markup_pct`` markup is applied to whichever base is used.
    """
    cfg = settings or get_settings()

    if provider_cost_usd is not None:
        return _apply_markup(float(provider_cost_usd), cfg)

    if not input_tokens and not output_tokens:
        return None

    pricing = _resolve_pricing(model_name, cfg)
    if pricing is None:
        return None

    input_rate, output_rate = pricing
    cost = (int(input_tokens or 0) / 1_000_000) * input_rate + (int(output_tokens or 0) / 1_000_000) * output_rate
    return _apply_markup(cost, cfg)
