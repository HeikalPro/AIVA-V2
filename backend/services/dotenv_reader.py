"""Read individual variables from the project .env files (llm_service/.env, root .env).

Kept dependency-free so low-level services (e.g. the SovereignEG catalog) can read the
SovereignEG key/base URL without importing heavier modules and risking import cycles.
"""

from __future__ import annotations

import os
from pathlib import Path

_LLM_SERVICE_ENV = Path(__file__).resolve().parents[2] / "llm_service" / ".env"
_ROOT_ENV = Path(__file__).resolve().parents[2] / ".env"


def _read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_dotenv(path: Path, name: str) -> str:
    if path.is_file():
        for line in _read_text_file(path).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() != name:
                continue
            return value.strip().strip('"').strip("'")
    return os.environ.get(name, "").strip()


def read_llm_service_env(name: str) -> str:
    """Read one variable from llm_service/.env (SovereignEG key, base URL, etc.)."""
    return read_dotenv(_LLM_SERVICE_ENV, name)


def read_root_env(name: str) -> str:
    """Read one variable from the root AIVA-V2/.env."""
    return read_dotenv(_ROOT_ENV, name)
