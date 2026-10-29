from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).parent.parent
_SERVICE_DIR = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Root .env carries shared keys (ORACLE_*, OPENAI_API_KEY, POOL_*).
        # embedding_service/.env carries service-only keys and can override root.
        env_file=(_ROOT / ".env", _SERVICE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    oracle_dsn: str = Field(
        default="localhost:1521/FREEPDB1",
        description="Oracle connect descriptor or Easy Connect string",
    )
    oracle_user: str = "kb_user"
    oracle_password: str = "change_me"

    oracle_wallet_dir: str | None = None
    oracle_wallet_password: str | None = None

    pool_min: int = 1
    pool_max: int = 8

    oracle_ping_interval: int = Field(
        default=10,
        ge=0,
        description=(
            "Seconds a pooled connection may sit idle before it is pinged on acquire. "
            "The KB pool often points at a remote DSN, where idle connections are silently "
            "dropped by NAT/firewalls; without a ping the next caller gets a dead connection. "
            "0 = ping on every acquire. Set via ORACLE_PING_INTERVAL."
        ),
    )

    oracle_call_timeout_ms: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Optional Oracle round-trip limit per call (ms). "
            "0 = driver default (no limit). Use e.g. 120000 to fail fast instead of hanging. "
            "Set via ORACLE_CALL_TIMEOUT_MS."
        ),
    )

    api_host: str = "0.0.0.0"
    api_port: int = 8080

    admin_api_key: str | None = Field(
        default=None,
        description="If set, require X-API-Key header for mutating routes",
    )

    redis_url: str | None = Field(
        default=None,
        description="If set, ingest/reindex jobs are enqueued for workers",
    )
    redis_job_queue: str = "embedding_service:jobs"

    default_openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("DEFAULT_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )

    search_default_top_k: int = 10
    search_max_top_k: int = 50

    ingest_batch_embed_size: int = 64
    job_poll_interval_sec: float = 0.5

    ingest_verbose_log: bool = Field(
        default=False,
        description="Per-line/per-merge ingest timings and flush logs to terminal (INGEST_VERBOSE_LOG)",
    )

    embedding_default_usd_per_million_tokens: float | None = Field(
        default=None,
        description=(
            "Default USD per 1M tokens when corpus embedder.pricing_usd_per_million_tokens is unset. "
            "Example for OpenAI text-embedding-3-small: 0.02"
        ),
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
