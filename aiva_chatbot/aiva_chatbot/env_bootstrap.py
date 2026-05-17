"""Load monorepo ``.env`` files into ``os.environ`` before LLM / embedding settings resolve.

When ``llm_service`` is installed from PyPI or ``site-packages``, its built-in ``env_file``
paths point at the install directory, not your repo. Preloading repo and service ``.env``
files fixes missing ``OPENAI_API_KEY`` / Oracle settings for ``uvicorn`` run from ``AIVA-V2``.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)
_loaded = False


def monorepo_root() -> Path:
    """``AIVA-V2`` root (parent of the ``aiva_chatbot`` project folder)."""
    return Path(__file__).resolve().parent.parent.parent


def preload_monorepo_dotenv(*, override: bool = True) -> None:
    """Merge env files (later paths override earlier keys when ``override`` is true)."""
    global _loaded
    if _loaded:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    root = monorepo_root()
    candidates = [
        root / ".env",
        root / "embedding_service" / ".env",
        root / "llm_service" / ".env",
        root / "aiva_chatbot" / ".env",
    ]
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=override)
            _log.debug("Loaded environment from %s", path)
    _loaded = True
