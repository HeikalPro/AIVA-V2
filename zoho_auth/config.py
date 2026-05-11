"""Configuration objects and loaders.

``ZohoConfig`` is an immutable value object. ``ConfigLoader`` is the contract;
``EnvConfigLoader`` is the default implementation that reads ``.env`` from the
``zoho_auth`` package directory (next to ``config.py``), unless you pass a
different ``dotenv_path``.
A team member who needs to load configuration from somewhere else (a vault, a
database, AWS Secrets Manager, ...) can implement ``ConfigLoader`` and pass it
to the ``ServiceContainer``.
"""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Protocol

from dotenv import load_dotenv

from .exceptions import ZohoConfigError

PACKAGE_ROOT = Path(__file__).resolve().parent

DEFAULT_SESSION_MAX_AGE_DAYS = 7
DEFAULT_TOKEN_STORE_FILENAME = ".zoho_login_tokens.json"


@dataclass(frozen=True)
class ZohoConfig:
    """Immutable Zoho OAuth configuration."""

    client_id: str
    client_secret: str
    redirect_uri: str
    auth_url: str
    token_url: str
    user_info_url: str
    scope: str
    allowed_email_domains: tuple = ()
    session_max_age_days: int = DEFAULT_SESSION_MAX_AGE_DAYS
    token_store_path: Optional[str] = None

    @property
    def callback_host(self) -> str:
        return urllib.parse.urlparse(self.redirect_uri).hostname or "localhost"

    @property
    def callback_port(self) -> int:
        port = urllib.parse.urlparse(self.redirect_uri).port
        if port:
            return port
        return 443 if self.redirect_uri.startswith("https") else 80

    @property
    def callback_path(self) -> str:
        return urllib.parse.urlparse(self.redirect_uri).path or "/"


class ConfigLoader(Protocol):
    """Contract for anything that produces a ``ZohoConfig``."""

    def load(self) -> ZohoConfig: ...


class EnvConfigLoader:
    """Default loader. Reads ``zoho_auth/.env`` plus the process environment."""

    _REQUIRED = {
        "client_id": "ZOHO_CLIENT_ID",
        "client_secret": "ZOHO_CLIENT_SECRET",
        "redirect_uri": "ZOHO_REDIRECT_URI",
        "auth_url": "ZOHO_AUTH_URL",
        "token_url": "ZOHO_TOKEN_URL",
        "user_info_url": "ZOHO_USER_INFO_URL",
        "scope": "ZOHO_SCOPE",
    }

    def __init__(self, dotenv_path: Optional[str] = None) -> None:
        self._dotenv_path = dotenv_path

    def load(self) -> ZohoConfig:
        env_file = self._dotenv_path
        if env_file is None:
            env_file = str(PACKAGE_ROOT / ".env")
        load_dotenv(dotenv_path=env_file, override=True)

        values: dict = {}
        missing: List[str] = []
        for attr, env_key in self._REQUIRED.items():
            value = self._clean(os.getenv(env_key))
            if not value:
                missing.append(env_key)
            values[attr] = value

        if missing:
            raise ZohoConfigError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        domains = self._parse_domains(os.getenv("ZOHO_ALLOWED_EMAIL_DOMAIN"))
        values["allowed_email_domains"] = domains
        values["session_max_age_days"] = self._parse_session_max_age(
            os.getenv("ZOHO_SESSION_MAX_AGE_DAYS")
        )
        values["token_store_path"] = self._resolve_token_store_path(
            os.getenv("ZOHO_TOKEN_STORE_PATH")
        )
        return ZohoConfig(**values)

    @staticmethod
    def _clean(value: Optional[str]) -> str:
        if not value:
            return ""
        return value.strip().strip('"').strip("'")

    @classmethod
    def _parse_domains(cls, raw: Optional[str]) -> tuple:
        cleaned = cls._clean(raw)
        if not cleaned:
            return ()
        parts = [p.strip().lower().lstrip("@") for p in cleaned.split(",")]
        return tuple(p for p in parts if p)

    @classmethod
    def _parse_session_max_age(cls, raw: Optional[str]) -> int:
        cleaned = cls._clean(raw)
        if not cleaned:
            return DEFAULT_SESSION_MAX_AGE_DAYS
        try:
            value = int(cleaned)
        except ValueError as exc:
            raise ZohoConfigError(
                f"ZOHO_SESSION_MAX_AGE_DAYS must be an integer, got '{cleaned}'."
            ) from exc
        if value < 0:
            raise ZohoConfigError("ZOHO_SESSION_MAX_AGE_DAYS cannot be negative.")
        return value

    @classmethod
    def _resolve_token_store_path(cls, raw: Optional[str]) -> str:
        cleaned = cls._clean(raw)
        if cleaned:
            return str(Path(cleaned).expanduser().resolve())
        return str(PACKAGE_ROOT / DEFAULT_TOKEN_STORE_FILENAME)
