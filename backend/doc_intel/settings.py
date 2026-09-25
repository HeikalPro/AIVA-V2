"""Settings for the document intelligence layer (``DOC_INTEL_*`` environment variables).

Reads the same ``.env`` files as ``backend.config`` so a deployment configures the
module in one place. Every default is safe: nothing here enables automation.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parent.parent.parent
_BACKEND_DIR = _ROOT / "backend"


class DocIntelSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_ROOT / ".env", _BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        env_prefix="DOC_INTEL_",
        extra="ignore",
    )

    # Mount the routers and run the import worker. Off = the module is inert.
    enabled: bool = True

    # Uploaded originals + normalized JSON. Relative paths resolve against AIVA-V2/.
    storage_dir: str = "data/doc_intel"
    max_upload_mb: int = Field(default=50, ge=1, le=300)
    max_files_per_upload: int = Field(default=20, ge=1, le=100)

    # Extraction (document-extractor runs in a child process).
    max_pages: int = Field(default=300, ge=1, le=5000)
    extraction_timeout_seconds: int = Field(default=900, ge=30, le=7200)
    extraction_mode: str = "balanced"  # fast | balanced | accurate
    ocr_languages: str = "ara,eng"  # Tesseract codes, comma separated
    tesseract_cmd: str | None = None  # full path when tesseract is not on PATH

    # Chunking / embedding limits (per document).
    max_chunks: int = Field(default=2000, ge=1, le=20000)
    embed_batch_size: int = Field(default=64, ge=1, le=512)
    embed_max_retries: int = Field(default=3, ge=0, le=10)

    # Worker.
    worker_poll_seconds: float = Field(default=5.0, ge=0.5, le=300)
    stuck_after_minutes: int = Field(default=60, ge=5, le=1440)

    # Monitoring.
    health_stale_minutes: int = Field(default=10, ge=1, le=1440)
    health_min_interval_seconds: int = Field(default=30, ge=0, le=3600)
    health_check_timeout_seconds: float = Field(default=20.0, ge=1, le=300)
    # Kept well under nginx's 60 s proxy timeout: "Run checks now" waits for the slowest
    # check, and the smoke test normally finishes in about a second.
    extraction_smoke_timeout_seconds: float = Field(default=40.0, ge=5, le=600)

    timezone: str = "Africa/Cairo"

    # ---- Phase 2: SharePoint / OneDrive -> CRM ------------------------------------------------
    # Fernet key(s) that encrypt the stored Microsoft credentials: comma separated, the first
    # one encrypts, all of them decrypt (rotation). Generate with
    # ``python -m backend.doc_intel.crypto generate-key``. Never in the DB, the repo or .env.example.
    secrets_key: SecretStr | None = None
    # Microsoft endpoints (change only for sovereign clouds).
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_authority_host: str = "https://login.microsoftonline.com"
    graph_scope: str = "https://graph.microsoft.com/.default"
    graph_timeout_seconds: float = Field(default=30.0, ge=1, le=300)
    # A source's site URL must be https and its host must end with one of these (SSRF guard).
    sharepoint_host_suffixes: str = ".sharepoint.com"
    sync_max_files: int = Field(default=5000, ge=1, le=100000)
    sync_max_file_mb: int = Field(default=100, ge=1, le=1024)
    # Client CRM entity schemas (JSON files, comma separated) on top of the generic ones.
    crm_schema_paths: str = ""
    crm_min_confidence: float = Field(default=0.5, ge=0, le=1)
    # Automatic syncs + periodic health checks. Off by default: a deployment opts in.
    scheduler_enabled: bool = False
    scheduler_tick_seconds: int = Field(default=60, ge=10, le=3600)
    health_interval_minutes: int = Field(default=15, ge=1, le=1440)

    # Best-effort writes into the EXISTING AIVA_audit_logs / AIVA_error_logs tables
    # (admin actions, unexpected failures). Test harnesses turn these off so tests
    # never write to tables outside this module.
    audit_enabled: bool = True
    error_log_enabled: bool = True

    @property
    def storage_path(self) -> Path:
        p = Path(self.storage_dir)
        return p if p.is_absolute() else (_ROOT / p)

    @property
    def ocr_language_list(self) -> tuple[str, ...]:
        langs = tuple(x.strip() for x in self.ocr_languages.split(",") if x.strip())
        return langs or ("eng",)

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def sync_max_file_bytes(self) -> int:
        return self.sync_max_file_mb * 1024 * 1024

    @property
    def sharepoint_host_suffix_list(self) -> tuple[str, ...]:
        return tuple(s.strip().lower() for s in self.sharepoint_host_suffixes.split(",") if s.strip())

    @property
    def crm_schema_path_list(self) -> tuple[str, ...]:
        return tuple(s.strip() for s in self.crm_schema_paths.split(",") if s.strip())


@lru_cache
def get_doc_intel_settings() -> DocIntelSettings:
    return DocIntelSettings()
