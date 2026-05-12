"""YAML/JSON config file loading and secret masking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def env_file_candidates() -> tuple[str, ...]:
    """`.env` paths for pydantic-settings (earlier files lose to later on duplicate keys).

    Order: vendored project root (next to ``pyproject.toml``), package directory, then cwd
    so scripts run from a parent folder still pick up the library's ``.env``, while a cwd
    file can override.
    """
    here = Path(__file__).resolve()
    project_root = here.parents[2]
    package_root = here.parents[1]
    cwd = Path.cwd()
    return (
        str(project_root / ".env"),
        str(package_root / ".env"),
        str(cwd / ".env"),
    )

_REDACT_KEYS = frozenset(
    {"api_key", "authorization", "password", "secret", "token", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"}
)


def mask_secrets(obj: Any) -> Any:
    """Return a deep copy with sensitive keys redacted (for logging)."""
    if isinstance(obj, dict):
        return {
            k: "***REDACTED***" if k.lower() in {x.lower() for x in _REDACT_KEYS} else mask_secrets(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask_secrets(x) for x in obj]
    return obj


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load YAML or JSON config file into a dict."""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in {".yaml", ".yml"}:
        data = yaml.safe_load(text)
    elif p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"Unsupported config extension: {p.suffix}")
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping")
    return data
