"""Parse and persist per-account widget feature flags (JSON in CLOB)."""
from __future__ import annotations

import json
from typing import Any

from backend.schemas.widget_features import WidgetFeaturesConfig

_DEFAULT = WidgetFeaturesConfig()


def parse_widget_features(raw: object | None) -> WidgetFeaturesConfig:
    if raw is None:
        return _DEFAULT.model_copy(deep=True)
    text = raw.read() if hasattr(raw, "read") else raw
    if not text:
        return _DEFAULT.model_copy(deep=True)
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="replace")
    if isinstance(text, str):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return _DEFAULT.model_copy(deep=True)
    else:
        parsed = text
    if not isinstance(parsed, dict):
        return _DEFAULT.model_copy(deep=True)
    try:
        return WidgetFeaturesConfig.model_validate(parsed)
    except Exception:
        return _DEFAULT.model_copy(deep=True)


def dumps_widget_features(config: WidgetFeaturesConfig | dict[str, Any] | None) -> str | None:
    if config is None:
        return None
    if isinstance(config, dict):
        try:
            config = WidgetFeaturesConfig.model_validate(config)
        except Exception:
            return None
    payload = config.model_dump(exclude_none=True)
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)


def widget_features_out(config: WidgetFeaturesConfig) -> dict[str, Any]:
    return config.model_dump(exclude_none=True)
