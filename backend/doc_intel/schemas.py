"""API models for the document intelligence endpoints (``/api/doc-intel``).

All timestamps are UTC ISO-8601 strings with a ``Z`` suffix. The frontend mirrors
these in ``AIVA-V2-UI/src/types/api.ts``; change both together.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

from backend.doc_intel.constants import (
    CrmStageName,
    DocStatus,
    EntityStatus,
    FileState,
    FileStatus,
    HealthStatus,
    KbStageName,
    RunStatus,
    RunTrigger,
    SourceStatus,
    StageStatus,
)

# ---- Knowledge-document import -------------------------------------------------------------


class StageOut(BaseModel):
    name: KbStageName
    status: StageStatus
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class DocWarningOut(BaseModel):
    code: str
    message: str
    page: int | None = None


class KbDocumentOut(BaseModel):
    id: int
    batch_id: str | None = None
    account_id: int
    account_name: str | None = None
    organization_name: str | None = None
    corpus_id: str
    queue_keys: list[str] = Field(default_factory=list)
    # Labels resolved from the corpus queue catalog at read time; same order as queue_keys.
    queue_labels: list[str] = Field(default_factory=list)
    vertical: str | None = None
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    status: DocStatus
    # Always the five stages, in pipeline order.
    stages: list[StageOut]
    failed_stage: KbStageName | None = None
    error_message: str | None = None
    warnings: list[DocWarningOut] = Field(default_factory=list)
    page_count: int | None = None
    chunk_count: int | None = None
    tokens_used: int | None = None
    cost_usd: float | None = None
    attempts: int = 0
    # 1-based position among QUEUED documents (None unless status == QUEUED).
    queue_position: int | None = None
    uploaded_by: int | None = None
    uploaded_by_email: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    published_at: str | None = None


class KbDocumentListOut(BaseModel):
    items: list[KbDocumentOut]
    limit: int
    offset: int
    total: int


class KbUploadOut(BaseModel):
    batch_id: str
    accepted: int
    rejected: int
    # One entry per uploaded file, rejected files included (upload stage FAILED + reason).
    documents: list[KbDocumentOut]


class KbQueuesUpdate(BaseModel):
    queue_keys: list[str] = Field(min_length=1, max_length=50)


class KbPreviewPage(BaseModel):
    number: int
    text: str


class KbPreviewOut(BaseModel):
    document_id: int
    page_count: int | None = None
    truncated: bool = False
    pages: list[KbPreviewPage] = Field(default_factory=list)
    extractor: dict[str, Any] = Field(default_factory=dict)


# ---- Module status -------------------------------------------------------------------------


class DocIntelStatusOut(BaseModel):
    enabled: bool
    # True when migration V001's tables exist.
    installed: bool
    schema_version: str | None = None
    worker_running: bool = False
    extraction_available: bool = False
    extraction_unavailable_reason: str | None = None
    detail: str | None = None
    # Upload limits, so the UI can pre-check files before sending them.
    max_upload_mb: int = 50
    max_files_per_upload: int = 20
    allowed_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".docx"])
    # Phase 2 (SharePoint -> CRM): True when migration V002's tables exist.
    crm_installed: bool = False
    sync_worker_running: bool = False
    scheduler_enabled: bool = False
    # False when DOC_INTEL_SECRETS_KEY is missing/invalid: credentials cannot be saved or read.
    secrets_key_configured: bool = False


# ---- Monitoring ----------------------------------------------------------------------------


class HealthComponentOut(BaseModel):
    key: str
    label: str
    status: HealthStatus
    reason: str | None = None
    suggested_action: str | None = None
    checked_at: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    consecutive_failures: int = 0
    latency_ms: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthOverviewOut(BaseModel):
    # FAILED when any component is FAILED; NOT_CONFIGURED components do not count.
    overall: Literal["HEALTHY", "FAILED"]
    checked_at: str | None = None
    # True when there are no stored results or the newest is older than DOC_INTEL_HEALTH_STALE_MINUTES.
    stale: bool = False
    # True when POST /health/run was ignored because checks ran too recently.
    throttled: bool = False
    components: list[HealthComponentOut]


class HealthEventOut(BaseModel):
    id: int
    component_key: str
    label: str
    old_status: HealthStatus | None = None
    new_status: HealthStatus
    reason: str | None = None
    created_at: str | None = None


class FailureItemOut(BaseModel):
    kind: Literal["kb_document", "crm_file", "sync_run"]
    id: int
    title: str
    account_name: str | None = None
    stage: str | None = None
    reason: str | None = None
    occurred_at: str | None = None


class FailuresOut(BaseModel):
    days: int
    items: list[FailureItemOut]


class ActivityItemOut(BaseModel):
    kind: Literal["kb_document", "health", "crm_file", "sync_run"]
    level: Literal["info", "warning", "error"]
    message: str
    ref_id: int | None = None
    occurred_at: str | None = None


class ActivityOut(BaseModel):
    items: list[ActivityItemOut]


# ---- SharePoint / OneDrive -> CRM (Flow 2) --------------------------------------------------


class SyncRunOut(BaseModel):
    id: int
    source_id: int
    trigger_type: RunTrigger
    triggered_by: int | None = None
    triggered_by_email: str | None = None
    status: RunStatus
    files_seen: int = 0
    files_new: int = 0
    files_changed: int = 0
    files_deleted: int = 0
    files_unchanged: int = 0
    files_failed: int = 0
    error_message: str | None = None
    # e.g. {"listing_complete": bool, "truncated_reason": str | None, "seconds": float}
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class SyncRunListOut(BaseModel):
    items: list[SyncRunOut]
    limit: int
    offset: int
    total: int


class SourceOut(BaseModel):
    id: int
    name: str
    account_id: int | None = None
    account_name: str | None = None
    provider: str = "microsoft_graph"
    # Decrypted identifiers, visible to Super Admin + Developer. The secret itself is NEVER returned.
    tenant_id: str | None = None
    client_id: str | None = None
    client_secret_set: bool = False
    client_secret_hint: str | None = None  # e.g. "…abcd"
    secret_updated_at: str | None = None
    # False when the stored credentials cannot be decrypted (key missing or rotated away).
    credentials_readable: bool = True
    site_url: str | None = None
    drive_name: str | None = None
    folder_path: str | None = None
    recursive: bool = True
    file_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".docx"])
    use_intelligence: bool = False
    sync_enabled: bool = False
    sync_interval_days: int = 14
    sync_hour: int = 2
    next_sync_at: str | None = None
    last_sync_at: str | None = None
    last_sync_status: RunStatus | None = None
    last_sync_error: str | None = None
    last_success_at: str | None = None
    status: SourceStatus = "ACTIVE"
    # The QUEUED/RUNNING run, if any ("Sync now" is disabled while one exists).
    active_run: SyncRunOut | None = None
    # {"files_active", "files_failed", "files_deleted", "entities_active"}
    counts: dict[str, int] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    account_id: int | None = None
    tenant_id: str = Field(min_length=1, max_length=200)
    client_id: str = Field(min_length=1, max_length=200)
    client_secret: SecretStr = Field(min_length=1, max_length=1024)
    # https://<tenant>.sharepoint.com/sites/<site> (or a OneDrive for Business personal site).
    site_url: str = Field(min_length=1, max_length=1000)
    # Document library name; None = the default library of the site ("Documents").
    drive_name: str | None = Field(default=None, max_length=200)
    # Folder inside the library, e.g. "/CRM/Contracts"; None or "/" = the library root.
    folder_path: str | None = Field(default=None, max_length=1000)
    recursive: bool = True
    file_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".docx"], min_length=1, max_length=20)
    sync_enabled: bool = False
    sync_interval_days: int = Field(default=14, ge=1, le=365)
    sync_hour: int = Field(default=2, ge=0, le=23)


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    account_id: int | None = None
    tenant_id: str | None = Field(default=None, min_length=1, max_length=200)
    client_id: str | None = Field(default=None, min_length=1, max_length=200)
    # Omit (or null) to keep the stored secret.
    client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    site_url: str | None = Field(default=None, min_length=1, max_length=1000)
    drive_name: str | None = Field(default=None, max_length=200)
    folder_path: str | None = Field(default=None, max_length=1000)
    recursive: bool | None = None
    file_extensions: list[str] | None = Field(default=None, min_length=1, max_length=20)
    sync_enabled: bool | None = None
    sync_interval_days: int | None = Field(default=None, ge=1, le=365)
    sync_hour: int | None = Field(default=None, ge=0, le=23)
    status: Literal["ACTIVE", "DISABLED"] | None = None


class ConnectionStepOut(BaseModel):
    # credentials | token | site | drive | folder | listing
    key: str
    label: str
    ok: bool
    latency_ms: int | None = None
    detail: str | None = None
    suggested_action: str | None = None


class ConnectionTestOut(BaseModel):
    ok: bool
    steps: list[ConnectionStepOut]
    checked_at: str | None = None
    # A few file names found in the folder (only when listing succeeded).
    sample_files: list[str] = Field(default_factory=list)
    files_found: int | None = None


class CrmStageOut(BaseModel):
    name: CrmStageName
    status: StageStatus
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class SourceFileOut(BaseModel):
    id: int
    source_id: int
    name: str | None = None
    path: str | None = None
    web_url: str | None = None
    size_bytes: int | None = None
    modified_at: str | None = None
    state: FileState
    status: FileStatus
    # Always the five stages, in pipeline order.
    stages: list[CrmStageOut]
    failed_stage: CrmStageName | None = None
    error_message: str | None = None
    warnings: list[DocWarningOut] = Field(default_factory=list)
    entity_count: int | None = None
    is_valid: bool | None = None
    attempts: int = 0
    last_run_id: int | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    processed_at: str | None = None
    deleted_at: str | None = None
    updated_at: str | None = None


class SourceFileListOut(BaseModel):
    items: list[SourceFileOut]
    limit: int
    offset: int
    total: int


class CrmEntityOut(BaseModel):
    id: int
    source_id: int
    source_name: str | None = None
    source_file_id: int
    file_name: str | None = None
    account_id: int | None = None
    entity_type: str
    display_value: str | None = None
    confidence: float | None = None
    is_valid: bool | None = None
    # field -> value (the CRM record, without "_meta")
    fields: dict[str, Any] = Field(default_factory=dict)
    # the "_meta" block: per-field confidence, extractor and provenance (page, block, source text)
    provenance: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    status: EntityStatus
    created_at: str | None = None
    updated_at: str | None = None
    withdrawn_at: str | None = None


class CrmEntityListOut(BaseModel):
    items: list[CrmEntityOut]
    limit: int
    offset: int
    total: int
