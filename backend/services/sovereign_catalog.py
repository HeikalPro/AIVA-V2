"""Fetch the SovereignEG model catalog (with live per-model pricing).

SovereignEG's OpenAI-compatible ``GET /models`` returns a ``pricing`` block per
model (``input_per_1m_egp`` / ``output_per_1m_egp``, in EGP). We fetch it, normalize
it, and cache it briefly so the LLM Configs page can show each model's price without
hammering the provider on every render.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from backend.config import Settings, get_settings
from backend.services.dotenv_reader import read_llm_service_env as _read_llm_service_env
from backend.services.llm_cost import apply_cost_markup

_log = logging.getLogger(__name__)

_DEFAULT_BASE = "https://backend.sovereigneg.com/v1"
# Prices only change on the daily scheduled refresh (22:00 Cairo). A long TTL keeps the
# lazy cache stable during the day; a cold-started process still fetches once on demand.
_CACHE_TTL_SECONDS = 24 * 3600
_KEY_ENV_NAMES = ("SOVEREIGNEG_API_KEY", "LLM_CHAT_API_KEY", "OPENAI_API_KEY")

# Daily refresh schedule.
_CAIRO_TZ = ZoneInfo("Africa/Cairo")
_REFRESH_HOUR = 22  # 10:00 PM Cairo (DST handled by zoneinfo)

# Process-wide cache + last fetch outcome so the page can show why a fetch failed.
#   at:              monotonic seconds of the last successful fetch (for TTL)
#   data:            last successfully fetched models, or None
#   last_success_at: ISO wall-clock time of the last successful fetch
#   error:           message from the most recent failed fetch, cleared on success
#   error_at:        ISO wall-clock time of that failure
_cache: dict[str, Any] = {"at": 0.0, "data": None, "last_success_at": None, "error": None, "error_at": None}


def _base_url() -> str:
    raw = _read_llm_service_env("SOVEREIGNEG_BASE_URL") or _read_llm_service_env("OPENAI_BASE_URL")
    return (raw or _DEFAULT_BASE).rstrip("/")


def _api_key() -> str:
    for name in _KEY_ENV_NAMES:
        raw = _read_llm_service_env(name)
        if raw:
            return raw
    return ""


def _normalize(model: dict[str, Any]) -> dict[str, Any]:
    pricing = model.get("pricing") or {}
    return {
        "id": model.get("id"),
        "display_name": model.get("display_name") or model.get("id"),
        "provider": model.get("provider") or model.get("owned_by"),
        "modality": model.get("modality"),
        "context_window": model.get("context_window"),
        "status": model.get("status"),
        "input_per_1m_egp": pricing.get("input_per_1m_egp"),
        "output_per_1m_egp": pricing.get("output_per_1m_egp"),
        "currency": "EGP",
    }


async def get_sovereign_catalog(*, force: bool = False) -> list[dict[str, Any]]:
    """Return the normalized SovereignEG model catalog, served from cache when fresh.

    On any failure (no key, network error) returns the last cached value, or an empty
    list — the UI degrades to "price unavailable" rather than erroring.
    """
    now = time.monotonic()
    cached = _cache.get("data")
    if not force and cached is not None and (now - float(_cache.get("at") or 0.0)) < _CACHE_TTL_SECONDS:
        return cached

    def _fail(msg: str) -> list[dict[str, Any]]:
        _log.warning("SovereignEG catalog fetch failed: %s", msg)
        _cache["error"] = msg
        _cache["error_at"] = datetime.now(UTC).isoformat()
        return cached or []

    key = _api_key()
    if not key:
        return _fail("SovereignEG API key not configured (SOVEREIGNEG_API_KEY).")

    url = f"{_base_url()}/models"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:200] if exc.response is not None else ""
        return _fail(f"HTTP {exc.response.status_code} from {url}: {body}".strip())
    except httpx.HTTPError as exc:
        return _fail(f"Network error reaching {url}: {exc}")
    except Exception as exc:  # noqa: BLE001 - never break the page
        return _fail(f"Unexpected error: {exc}")

    models = [_normalize(m) for m in (payload.get("data") or []) if m.get("id")]
    _cache["data"] = models
    _cache["at"] = now
    _cache["last_success_at"] = datetime.now(UTC).isoformat()
    _cache["error"] = None
    _cache["error_at"] = None
    return models


async def get_sovereign_catalog_status(*, force: bool = False) -> dict[str, Any]:
    """Catalog plus fetch outcome, so the UI can show an error/stale banner.

    ``stale`` is True when the fetch failed but we're serving an older cached copy.
    """
    items = await get_sovereign_catalog(force=force)
    error = _cache.get("error")
    return {
        "items": items,
        "error": error,
        "error_at": _cache.get("error_at"),
        "last_success_at": _cache.get("last_success_at"),
        "stale": bool(error) and bool(items),
    }


def _seconds_until_next_refresh(now_utc: datetime) -> float:
    """Seconds from now until the next 22:00 Africa/Cairo (today if still ahead, else tomorrow)."""
    now_cairo = now_utc.astimezone(_CAIRO_TZ)
    target = now_cairo.replace(hour=_REFRESH_HOUR, minute=0, second=0, microsecond=0)
    if target <= now_cairo:
        target += timedelta(days=1)
    return max(1.0, (target - now_cairo).total_seconds())


async def _daily_refresh_loop() -> None:
    # Warm the cache once on startup (best-effort) so the first page load is instant.
    try:
        await get_sovereign_catalog()
    except Exception:  # noqa: BLE001
        pass
    while True:
        delay = _seconds_until_next_refresh(datetime.now(UTC))
        _log.info("Next SovereignEG price refresh in %.0f min (22:00 Africa/Cairo)", delay / 60)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            break
        try:
            models = await get_sovereign_catalog(force=True)
            _log.info("SovereignEG catalog refreshed on schedule: %d models", len(models))
        except Exception as exc:  # noqa: BLE001
            _log.warning("Scheduled SovereignEG catalog refresh failed: %s", exc)


def start_catalog_scheduler() -> asyncio.Task[None]:
    """Start the daily 22:00-Cairo catalog refresh loop. Returns the task so it can be cancelled."""
    return asyncio.create_task(_daily_refresh_loop(), name="sovereign-catalog-refresh")


async def get_model_egp_pricing(model_name: str) -> tuple[float, float] | None:
    """Return (input_per_1m_egp, output_per_1m_egp) for a model from the cached catalog.

    Matches the model id case-insensitively. Missing rates default to 0.0. Returns None
    when the model is not in the catalog (or the catalog is unavailable).
    """
    if not model_name:
        return None
    target = model_name.strip().lower()
    for item in await get_sovereign_catalog():
        if str(item.get("id") or "").lower() == target:
            return (float(item.get("input_per_1m_egp") or 0.0), float(item.get("output_per_1m_egp") or 0.0))
    return None


async def estimate_llm_cost_egp(
    *,
    model_name: str,
    input_tokens: int | None,
    output_tokens: int | None,
    provider_cost: float | None = None,
    settings: Settings | None = None,
) -> float | None:
    """Estimated request cost in EGP (with markup), using live SovereignEG prices.

    Prefers a provider-reported cost when present; otherwise multiplies token counts by
    the model's SovereignEG EGP rates. Returns None when the model/pricing is unknown.
    """
    cfg = settings or get_settings()

    if provider_cost is not None:
        return apply_cost_markup(float(provider_cost), cfg)

    if not input_tokens and not output_tokens:
        return None

    pricing = await get_model_egp_pricing(model_name)
    if pricing is None:
        return None

    input_rate, output_rate = pricing
    cost = (int(input_tokens or 0) / 1_000_000) * input_rate + (int(output_tokens or 0) / 1_000_000) * output_rate
    return apply_cost_markup(cost, cfg)
