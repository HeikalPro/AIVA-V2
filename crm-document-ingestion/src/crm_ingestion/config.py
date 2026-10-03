"""Settings from environment variables (and an optional .env file).

Nothing here is required for local development: the Microsoft credentials default to
empty and are only checked when a SharePoint call is actually made."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MicrosoftGraphSettings(BaseSettings):
    """Entra ID app registration and Graph endpoint. Placeholders until credentials exist."""

    model_config = SettingsConfigDict(env_prefix="MICROSOFT_", env_file=".env", extra="ignore")

    tenant_id: str = ""
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    authority_host: str = "https://login.microsoftonline.com"
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_timeout_seconds: float = 30.0
    max_download_bytes: int = Field(default=100 * 1024 * 1024, gt=0)

    @property
    def is_configured(self) -> bool:
        """True when all three credentials are set."""
        return bool(self.tenant_id and self.client_id and self.client_secret.get_secret_value())

    def missing(self) -> list[str]:
        """Names of the environment variables still empty."""
        out: list[str] = []
        if not self.tenant_id:
            out.append("MICROSOFT_TENANT_ID")
        if not self.client_id:
            out.append("MICROSOFT_CLIENT_ID")
        if not self.client_secret.get_secret_value():
            out.append("MICROSOFT_CLIENT_SECRET")
        return out


class ExtractionSettings(BaseSettings):
    """How this project calls document-extractor. DOCUMENT_EXTRACTOR_CONFIG, when set,
    is read by the library itself and wins over `mode`."""

    model_config = SettingsConfigDict(env_prefix="CRM_EXTRACTION_", env_file=".env", extra="ignore")

    mode: Literal["fast", "balanced", "accurate"] = "balanced"


class CRMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CRM_", env_file=".env", extra="ignore")

    min_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # JSON files of client entity schemas, registered on top of the generic ones.
    # Env value is a JSON list, e.g. CRM_SCHEMA_PATHS=["schemas/client.json"]
    schema_paths: list[Path] = Field(default_factory=list)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    microsoft: MicrosoftGraphSettings = Field(default_factory=MicrosoftGraphSettings)
    extraction: ExtractionSettings = Field(default_factory=ExtractionSettings)
    crm: CRMSettings = Field(default_factory=CRMSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, read once. Tests build `Settings(...)` directly instead."""
    return Settings()
