"""Shared test setup: isolate every test from the developer's environment and .env."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from crm_ingestion.config import get_settings

_ENV_PREFIXES = ("MICROSOFT_", "CRM_")
_ENV_NAMES = ("DOCUMENT_EXTRACTOR_CONFIG",)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No MICROSOFT_* / CRM_* / DOCUMENT_EXTRACTOR_CONFIG variables, and a cwd without a
    .env file (settings read `.env` relative to the cwd), so settings are the defaults."""
    for name in list(os.environ):
        upper = name.upper()
        if upper.startswith(_ENV_PREFIXES) or upper in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
