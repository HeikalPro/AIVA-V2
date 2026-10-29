"""Pure transforms of ``kb_corpus.config_json`` for queue availability (no I/O).

A queue is a key of ``config_json.queue_groups`` -> ``{label, verticals[]}``. An imported
document is made searchable for a queue by appending its own vertical (``kbdoc-<id>``) to
that queue's ``verticals``; chat retrieval then finds it through the existing
``JSON_VALUE(payload_json, '$.vertical') IN (...)`` filter, unchanged.

Every function returns a NEW dict and preserves everything it does not own: unknown
top-level keys, unknown per-queue keys (``label``, ``ivr_hint``, ...), queue order and the
other verticals. The catalog view (which keys exist, which verticals they resolve to) is
always the one retrieval uses: ``backend.services.kb_queue_groups.get_queue_groups_from_config``.
"""
from __future__ import annotations

import copy
from decimal import Decimal
from typing import Any

from backend.services.kb_queue_groups import DEFAULT_QUEUE_GROUPS, get_queue_groups_from_config


class UnknownQueueError(Exception):
    """A selected queue key is not (or no longer) a usable queue of the corpus."""

    def __init__(self, key: str) -> None:
        super().__init__(f"Unknown queue: {key}")
        self.key = key


def to_plain(obj: Any) -> Any:
    """Recursively turn ``Decimal`` into int/float (lists, tuples and dicts are copied).

    oracledb returns a native JSON column as a dict whose numbers are ``Decimal``;
    ``json.dumps`` cannot serialize those, so call this before writing a config back.
    Integral values written without a fraction (``2000``) become int, others float.
    """
    if isinstance(obj, Decimal):
        if obj.is_finite() and obj == obj.to_integral_value() and obj.as_tuple().exponent >= 0:
            return int(obj)
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_plain(v) for v in obj]
    return obj


def _is_valid_entry(key: Any, value: Any) -> bool:
    # Same acceptance rule as get_queue_groups_from_config.
    return isinstance(value, dict) and bool(str(key).strip()) and isinstance(value.get("verticals"), list)


def _catalog_index(groups: dict[str, Any]) -> dict[str, str]:
    """Catalog key (stripped, as retrieval sees it) -> raw key in ``groups``.

    When two raw keys strip to the same name, the later one wins, as in retrieval.
    """
    index: dict[str, str] = {}
    for raw_key, value in groups.items():
        if _is_valid_entry(raw_key, value):
            index[str(raw_key).strip()] = raw_key
    return index


def ensure_queue_groups(config: dict[str, Any] | None) -> dict[str, Any]:
    """Deep copy of ``config`` whose ``queue_groups`` exist explicitly.

    When retrieval would fall back to ``DEFAULT_QUEUE_GROUPS`` (``queue_groups`` missing,
    not a dict, empty, or without a single usable entry), those defaults are written
    into the copy so a vertical can be appended to them. The resulting catalog is the
    same one retrieval already served, so nothing changes for existing chats.
    Malformed entries retrieval ignores are kept as they are.
    """
    cfg = to_plain(copy.deepcopy(config)) if isinstance(config, dict) else {}
    raw = cfg.get("queue_groups")
    if not isinstance(raw, dict) or not raw:
        cfg["queue_groups"] = copy.deepcopy(DEFAULT_QUEUE_GROUPS)
    elif not _catalog_index(raw):
        merged = {k: v for k, v in raw.items() if k not in DEFAULT_QUEUE_GROUPS}
        merged.update(copy.deepcopy(DEFAULT_QUEUE_GROUPS))
        cfg["queue_groups"] = merged
    return cfg


def set_vertical_queues(config: dict[str, Any] | None, vertical: str, queue_keys: list[str]) -> dict[str, Any]:
    """Copy of ``config`` where ``vertical`` sits in EXACTLY the given queues.

    Adds it (once) to each selected queue and removes it from every other queue.
    Raises ``UnknownQueueError`` for a key that is not in the catalog, including a raw
    entry whose ``verticals`` is not a list (retrieval ignores such entries).
    """
    cfg = ensure_queue_groups(config)
    groups: dict[str, Any] = cfg["queue_groups"]
    index = _catalog_index(groups)
    selected_raw: set[str] = set()
    for key in queue_keys:
        name = str(key).strip()
        if name not in index:
            raise UnknownQueueError(name)
        selected_raw.add(index[name])

    for raw_key, entry in groups.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("verticals"), list):
            continue
        verticals: list[Any] = entry["verticals"]
        present = any(str(v) == vertical for v in verticals)
        if raw_key in selected_raw:
            if not present:
                verticals.append(vertical)
        elif present:
            entry["verticals"] = [v for v in verticals if str(v) != vertical]
    return cfg


def remove_vertical(config: dict[str, Any] | None, vertical: str) -> dict[str, Any]:
    """Copy of ``config`` with ``vertical`` removed from every queue (idempotent).

    Does not materialize default queue groups: a corpus without ``queue_groups`` cannot
    reference an imported document, so there is nothing to remove.
    """
    cfg = to_plain(copy.deepcopy(config)) if isinstance(config, dict) else {}
    groups = cfg.get("queue_groups")
    if not isinstance(groups, dict):
        return cfg
    for entry in groups.values():
        if isinstance(entry, dict) and isinstance(entry.get("verticals"), list):
            if any(str(v) == vertical for v in entry["verticals"]):
                entry["verticals"] = [v for v in entry["verticals"] if str(v) != vertical]
    return cfg


def queues_with_vertical(config: dict[str, Any] | None, vertical: str) -> list[str]:
    """Catalog keys (sorted) whose verticals include ``vertical``, as retrieval resolves them."""
    groups = get_queue_groups_from_config(to_plain(config) if isinstance(config, dict) else None)
    return sorted(key for key, group in groups.items() if vertical in group.get("verticals", []))


def queue_labels(config: dict[str, Any] | None, keys: list[str]) -> list[str]:
    """Display labels for ``keys`` (same order); a key that no longer exists shows as itself."""
    groups = get_queue_groups_from_config(to_plain(config) if isinstance(config, dict) else None)
    return [str(groups[k]["label"]) if k in groups else str(k) for k in keys]
