"""queue_groups transforms: the read-modify-write that makes a document visible to queues."""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from backend.doc_intel.queue_config import (
    UnknownQueueError,
    ensure_queue_groups,
    queue_labels,
    queues_with_vertical,
    remove_vertical,
    set_vertical_queues,
    to_plain,
)
from backend.services.kb_queue_groups import DEFAULT_QUEUE_GROUPS, resolve_verticals

from .conftest import corpus_config

V = "kbdoc-12"


def test_to_plain_converts_decimals_recursively():
    raw = {
        "chunk_max_chars": Decimal("2000"),
        "embedder": {"pricing_usd_per_million_tokens": Decimal("0.02"), "dimension": Decimal("1536")},
        "list": [Decimal("1"), Decimal("2.50"), "x", None, True],
        "sci": Decimal("1E+3"),
        "float_written": Decimal("2000.0"),
    }
    plain = to_plain(raw)
    assert plain["chunk_max_chars"] == 2000 and isinstance(plain["chunk_max_chars"], int)
    assert plain["embedder"]["pricing_usd_per_million_tokens"] == pytest.approx(0.02)
    assert isinstance(plain["embedder"]["dimension"], int)
    assert plain["list"] == [1, 2.5, "x", None, True]
    assert plain["sci"] == 1000 and isinstance(plain["sci"], int)
    assert plain["float_written"] == 2000.0 and isinstance(plain["float_written"], float)
    json.dumps(plain)  # serializable
    assert isinstance(raw["chunk_max_chars"], Decimal)  # input untouched


@pytest.mark.parametrize("groups", [None, "not-a-dict", {}, []])
def test_ensure_materializes_defaults_when_retrieval_would_fall_back(groups):
    cfg = {"adapter": "generic_jsonl_v1", "embedder": {"model": "m"}}
    if groups is not None:
        cfg["queue_groups"] = groups
    out = ensure_queue_groups(cfg)
    assert out["queue_groups"] == DEFAULT_QUEUE_GROUPS
    assert out["adapter"] == "generic_jsonl_v1" and out["embedder"] == {"model": "m"}
    out["queue_groups"]["HALAN"]["verticals"].append("x")
    assert "x" not in DEFAULT_QUEUE_GROUPS["HALAN"]["verticals"]  # defaults are copied, never aliased


def test_ensure_with_only_malformed_entries_uses_defaults_and_keeps_the_rest():
    cfg = {"queue_groups": {"Broken": {"label": "B"}, "Weird": "text"}}
    out = ensure_queue_groups(cfg)
    groups = out["queue_groups"]
    assert groups["Broken"] == {"label": "B"} and groups["Weird"] == "text"
    for key, value in DEFAULT_QUEUE_GROUPS.items():
        assert groups[key] == value


def test_ensure_keeps_a_valid_config_identical_but_copied():
    cfg = corpus_config()
    out = ensure_queue_groups(cfg)
    assert out == cfg
    assert out is not cfg and out["queue_groups"] is not cfg["queue_groups"]


def test_set_adds_vertical_to_selected_queues_only_and_preserves_everything():
    cfg = corpus_config()
    cfg["queue_groups"]["HALAN"]["future_field"] = {"a": 1}
    before = copy.deepcopy(cfg)
    out = set_vertical_queues(cfg, V, ["HALAN", "Cards"])
    assert out["queue_groups"]["HALAN"]["verticals"] == ["CF", "Pay", V]
    assert out["queue_groups"]["Cards"]["verticals"] == [V]
    assert out["queue_groups"]["Gomla"]["verticals"] == ["Gomla"]
    # unknown keys, labels and hints preserved
    assert out["custom_setting"] == {"keep": True}
    assert out["queue_groups"]["HALAN"]["ivr_hint"] == "press 1"
    assert out["queue_groups"]["HALAN"]["future_field"] == {"a": 1}
    assert out["queue_groups"]["Cards"]["label"] == "Card Support"
    assert list(out["queue_groups"]) == ["HALAN", "Gomla", "Cards"]  # order kept
    assert cfg == before  # input not mutated
    # retrieval (unchanged code) now resolves the document for exactly those queues
    assert V in resolve_verticals(out, ["HALAN"])
    assert V in resolve_verticals(out, ["Cards"])
    assert V not in resolve_verticals(out, ["Gomla"])


def test_set_is_idempotent():
    once = set_vertical_queues(corpus_config(), V, ["HALAN"])
    twice = set_vertical_queues(once, V, ["HALAN"])
    assert once == twice
    assert twice["queue_groups"]["HALAN"]["verticals"].count(V) == 1


def test_set_changes_queues_exactly():
    cfg = set_vertical_queues(corpus_config(), V, ["HALAN", "Gomla"])
    cfg = set_vertical_queues(cfg, V, ["Gomla", "Cards"])
    assert queues_with_vertical(cfg, V) == ["Cards", "Gomla"]
    assert cfg["queue_groups"]["HALAN"]["verticals"] == ["CF", "Pay"]


def test_set_does_not_touch_other_documents():
    cfg = set_vertical_queues(corpus_config(), "kbdoc-1", ["HALAN"])
    cfg = set_vertical_queues(cfg, "kbdoc-2", ["Gomla"])
    assert queues_with_vertical(cfg, "kbdoc-1") == ["HALAN"]
    assert queues_with_vertical(cfg, "kbdoc-2") == ["Gomla"]


def test_set_materializes_defaults_for_a_corpus_without_queue_groups():
    out = set_vertical_queues({"adapter": "halan_records_v1"}, V, ["Gomla"])
    assert out["queue_groups"]["Gomla"]["verticals"] == ["Gomla", V]
    assert out["queue_groups"]["HALAN"]["verticals"] == DEFAULT_QUEUE_GROUPS["HALAN"]["verticals"]
    assert out["adapter"] == "halan_records_v1"


def test_set_rejects_unknown_queue():
    with pytest.raises(UnknownQueueError) as info:
        set_vertical_queues(corpus_config(), V, ["HALAN", "Nope"])
    assert info.value.key == "Nope"


def test_set_rejects_entry_without_a_verticals_list():
    cfg = corpus_config()
    cfg["queue_groups"]["Broken"] = {"label": "Broken", "verticals": "CF"}
    with pytest.raises(UnknownQueueError):
        set_vertical_queues(cfg, V, ["Broken"])


def test_set_matches_keys_the_way_retrieval_strips_them():
    cfg = {"queue_groups": {" Spaced ": {"label": "S", "verticals": []}}}
    out = set_vertical_queues(cfg, V, ["Spaced"])
    assert out["queue_groups"][" Spaced "]["verticals"] == [V]


def test_set_accepts_decimal_configs_and_returns_json_safe_output():
    cfg = corpus_config()
    cfg["chunk_max_chars"] = Decimal("600")
    cfg["embedder"]["pricing_usd_per_million_tokens"] = Decimal("0.02")
    out = set_vertical_queues(cfg, V, ["HALAN"])
    assert json.loads(json.dumps(out))["chunk_max_chars"] == 600


def test_remove_vertical_everywhere_and_idempotent():
    cfg = set_vertical_queues(corpus_config(), V, ["HALAN", "Gomla", "Cards"])
    cfg["queue_groups"]["Gomla"]["verticals"].append(V)  # a duplicate from a manual edit
    out = remove_vertical(cfg, V)
    assert queues_with_vertical(out, V) == []
    assert out["queue_groups"]["HALAN"]["verticals"] == ["CF", "Pay"]
    assert remove_vertical(out, V) == out
    assert out["custom_setting"] == {"keep": True}


def test_remove_vertical_does_not_materialize_defaults():
    cfg = {"adapter": "halan_records_v1"}
    assert remove_vertical(cfg, V) == cfg


def test_queue_labels_follow_key_order_and_fall_back_to_the_key():
    assert queue_labels(corpus_config(), ["Cards", "HALAN", "Gone"]) == ["Card Support", "Halan", "Gone"]
    assert queue_labels(None, ["HALAN"]) == ["Halan"]  # defaults catalog
