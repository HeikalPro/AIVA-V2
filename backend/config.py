from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).parent.parent
_BACKEND_DIR = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_ROOT / ".env", _BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    backend_debug: bool = False
    backend_cors_origins: str = "http://localhost:3000,http://localhost:5173"

    oracle_dsn: str = "localhost:1521/FREEPDB1"
    oracle_user: str = "aiva_user"
    oracle_password: str = "change_me"
    oracle_wallet_dir: str | None = None
    oracle_wallet_password: str | None = None
    oracle_pool_min: int = 2
    oracle_pool_max: int = 20
    oracle_call_timeout_ms: int | None = None

    jwt_secret_key: str = "change-me-to-a-long-random-secret"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "HS256"

    rate_limit_default: str = "120/minute"
    rate_limit_login: str = "10/minute"

    llm_default_provider: str = "openai"
    llm_default_model: str = "gpt-4.1-mini"

    search_default_top_k: int = 10

    zoho_desk_api_base: str = "https://desk.zoho.com/api/v1"
    zoho_ticket_sync_enabled: bool = False
    zoho_frontend_redirect_url: str = "http://localhost:5173/auth/zoho-callback"
    zoho_auto_register_organization_id: int | None = None
    zoho_auto_register_role: str = "AGENT"

    notify_developers_enabled: bool = False
    notify_skip_creator: bool = True
    frontend_url: str = "http://localhost:5173"
    zoho_mail_api_base: str = "https://mail.zoho.com/api"
    zoho_mail_from_address: str = ""
    zoho_mail_account_id: str | None = None

    bootstrap_superadmin_enabled: bool = True
    bootstrap_superadmin_email: str = "admin@aiva.local"
    bootstrap_superadmin_password: str = "Admin@12345"
    bootstrap_superadmin_first_name: str = "AIVA"
    bootstrap_superadmin_last_name: str = "Super Admin"
    bootstrap_superadmin_organization_code: str = "AIVA"
    bootstrap_superadmin_organization_name: str = "AIVA"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
