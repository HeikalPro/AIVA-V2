"""KB queue groups: business queues (HALAN, Gomla, Tasaheel) mapped to chunk verticals."""
from __future__ import annotations

from typing import Any

from backend.exceptions import BadRequestError

DEFAULT_QUEUE_GROUPS: dict[str, dict[str, Any]] = {
    "HALAN": {
        "label": "Halan",
        "verticals": [
            "CF",
            "Pay",
            "General",
            "Halan",
            "Saving",
            "Gold",
            "Commerce",
            "Salary lending",
            "Irrelevant Call",
        ],
    },
    "Gomla": {
        "label": "Gomla",
        "verticals": ["Gomla"],
    },
    "Tasaheel": {
        "label": "Tasaheel",
        "verticals": ["Tasaheel"],
    },
}


def get_queue_groups_from_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not config:
        return dict(DEFAULT_QUEUE_GROUPS)
    raw = config.get("queue_groups")
    if not isinstance(raw, dict) or not raw:
        return dict(DEFAULT_QUEUE_GROUPS)
    merged: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        name = str(key).strip()
        if not name:
            continue
        verticals = value.get("verticals")
        if not isinstance(verticals, list):
            continue
        merged[name] = {
            "label": str(value.get("label") or name),
            "verticals": [str(v) for v in verticals if str(v).strip()],
        }
    return merged or dict(DEFAULT_QUEUE_GROUPS)


def list_queue_catalog(config: dict[str, Any] | None) -> list[dict[str, str]]:
    groups = get_queue_groups_from_config(config)
    return [{"key": key, "label": groups[key]["label"]} for key in sorted(groups.keys())]


def all_queue_keys(config: dict[str, Any] | None) -> list[str]:
    return [item["key"] for item in list_queue_catalog(config)]


def resolve_verticals(
    config: dict[str, Any] | None,
    active_queue_keys: list[str] | None,
) -> list[str] | None:
    """Return vertical list for search, or None for unrestricted (full corpus)."""
    if not active_queue_keys:
        return None
    groups = get_queue_groups_from_config(config)
    verticals: list[str] = []
    seen: set[str] = set()
    for key in active_queue_keys:
        group = groups.get(key)
        if not group:
            continue
        for v in group.get("verticals") or []:
            if v not in seen:
                seen.add(v)
                verticals.append(v)
    return verticals or None


def validate_active_queues(
    config: dict[str, Any] | None,
    active_queue_keys: list[str] | None,
    *,
    allowed_queue_keys: list[str] | None = None,
) -> list[str] | None:
    """Validate queue keys; return normalized list or None (unrestricted search)."""
    if active_queue_keys is None:
        return None
    if not active_queue_keys:
        return None

    groups = get_queue_groups_from_config(config)
    known = set(groups.keys())
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in active_queue_keys:
        key = str(raw).strip()
        if not key or key in seen:
            continue
        if key not in known:
            raise BadRequestError(f"Unknown queue: {key}")
        if allowed_queue_keys is not None and key not in allowed_queue_keys:
            raise BadRequestError(f"Queue not allowed for this agent: {key}")
        seen.add(key)
        normalized.append(key)
    if not normalized:
        return None
    return normalized


def parse_active_queues_json(raw: object | None) -> list[str] | None:
    if raw is None:
        return None
    text = raw.read() if hasattr(raw, "read") else raw
    if not text:
        return None
    if isinstance(text, str):
        import json

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
    else:
        parsed = text
    if not isinstance(parsed, list):
        return None
    keys = [str(x).strip() for x in parsed if str(x).strip()]
    return keys or None


def dumps_active_queues_json(keys: list[str] | None) -> str | None:
    if not keys:
        return None
    import json

    return json.dumps(keys, ensure_ascii=False)
