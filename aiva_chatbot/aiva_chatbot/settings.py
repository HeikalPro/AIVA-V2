"""HTTP API configuration (env-prefixed ``AIVA_``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_file_paths() -> tuple[str, ...]:
    """Resolve ``.env`` when the process cwd is not the ``aiva_chatbot`` project folder."""
    pkg_dir = Path(__file__).resolve().parent
    project_dir = pkg_dir.parent
    repo_dir = project_dir.parent
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in (Path.cwd() / ".env", project_dir / ".env", repo_dir / ".env"):
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if not candidate.is_file() or resolved in seen:
            continue
        seen.add(resolved)
        ordered.append(candidate)
    return tuple(str(p) for p in ordered)


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AIVA_",
        env_file=_env_file_paths(),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    corpus_id: str = Field(description="Default KB corpus id (hex) for vector search.")
    use_zoho_auth: bool = Field(
        default=False,
        description="If true, first chat triggers Zoho browser OAuth (usually off for APIs).",
    )
    cors_origins: str = Field(
        default="*",
        description="Comma-separated origins for CORS, or a single ``*``.",
    )

    @field_validator("corpus_id", mode="before")
    @classmethod
    def strip_corpus_id(cls, v: object) -> object:
        if isinstance(v, str):
            return v.strip()
        return v
