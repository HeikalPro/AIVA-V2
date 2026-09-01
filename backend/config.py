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
    backend_cors_origins: str = "http://localhost:3000,http://localhost:5173,http://localhost:5175,http://127.0.0.1:5173"

    oracle_dsn: str = "localhost:1521/FREEPDB1"
    oracle_user: str = "aiva_user"
    oracle_password: str = "change_me"
    oracle_wallet_dir: str | None = None
    oracle_wallet_password: str | None = None
    oracle_pool_min: int = 2
    oracle_pool_max: int = 20
    # Ping connections idle longer than this (seconds) before handing them out, so a
    # connection dropped while idle surfaces as a retry instead of a request failure.
    oracle_ping_interval: int = 10
    oracle_call_timeout_ms: int | None = None

    jwt_secret_key: str = "change-me-to-a-long-random-secret"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "HS256"

    rate_limit_default: str = "120/minute"
    rate_limit_login: str = "10/minute"
    rate_limit_signup: str = "30/hour"
    rate_limit_resend_otp: str = "20/hour"
    rate_limit_forgot_password: str = "10/hour"

    llm_default_provider: str = "openai"
    llm_default_model: str = "gpt-4.1-mini"
    llm_default_input_usd_per_million_tokens: float | None = None
    llm_default_output_usd_per_million_tokens: float | None = None
    # Fallback EGP rates used when a model has no SovereignEG catalog price, so every
    # model an account uses still contributes to the total cost instead of being dropped
    # as NULL. Leave unset to keep the old behavior (unpriced models stay uncounted).
    llm_default_input_egp_per_million_tokens: float | None = None
    llm_default_output_egp_per_million_tokens: float | None = None
    # Markup added on top of the raw LLM cost before it is reported/stored (e.g. 10 = +10%).
    llm_cost_markup_pct: float = 10.0

    # Shared secret the widget must send (X-Widget-Log-Secret header) to POST conversation
    # logs to /api/logs/widget-turn. Empty = widget logging disabled (endpoint rejects all).
    widget_log_secret: str = ""

    # Optional: aiva_chatbot service /health URL, for the Components dependency check.
    # Empty = not checked (reported as "not configured").
    chatbot_health_url: str = ""

    search_default_top_k: int = 10
    # Default KB link template when account.kb_source_base_url is empty. Use {id} or suffix with =.
    kb_source_base_url: str = "http://192.168.1.13/halankb/view.php?id="
    # Max source links per answer (from top RAG hits; retrieval may use more chunks for context).
    kb_source_max_count: int = 1

    zoho_desk_api_base: str = "https://desk.zoho.com/api/v1"
    zoho_ticket_sync_enabled: bool = False
    zoho_login_enabled: bool = False
    zoho_frontend_redirect_url: str = "http://localhost:5173/auth/zoho-callback"
    zoho_auto_register_organization_id: int | None = None
    zoho_auto_register_role: str = "AGENT"

    notify_developers_enabled: bool = False
    notify_skip_creator: bool = True
    # Email admins + developers when an unhandled server error is logged.
    notify_errors_enabled: bool = False
    # Suppress duplicate alerts for the same (exception type, route) within this window.
    notify_errors_throttle_seconds: int = 300
    frontend_url: str = "http://localhost:5173"
    # Logos shown in the email letterhead. Must be publicly reachable URLs — mail
    # clients fetch them over the internet, so localhost only works in local tests.
    # Empty falls back to <frontend_url>/GoChat247_blue_transparent.png.
    email_logo_url: str = ""
    # Optional AIVA wordmark artwork; empty renders the wordmark as styled text,
    # which also keeps the brand readable when a client blocks remote images.
    email_aiva_logo_url: str = ""
    # GoAI lockup shown at the foot of every email.
    # Empty falls back to <frontend_url>/GoAI_logo.png.
    email_footer_logo_url: str = ""
    zoho_mail_api_base: str = "https://mail.zoho.com/api"
    zoho_mail_from_address: str = ""
    zoho_mail_account_id: str | None = None

    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "GoAI247"
    smtp_encryption: str = "ssl"

    otp_expiry_minutes: int = 10
    otp_length: int = 6
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_max_resend_per_hour: int = 5

    login_max_failed_attempts: int = 5
    login_lock_minutes: int = 15

    password_min_length: int = 8
    password_require_complexity: bool = False

    signup_default_organization_id: int | None = None
    signup_default_role: str = "AGENT"

    app_name: str = "AIVA"
    app_url: str = "http://localhost:5175"

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
