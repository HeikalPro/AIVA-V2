"""Shared names for the document intelligence layer (statuses, stages, components)."""
from __future__ import annotations

from typing import Final, Literal

# ---- Knowledge-document import (Flow 1) -------------------------------------------------

KbStageName = Literal["upload", "extraction", "chunking", "embedding", "publishing"]
KB_STAGES: Final[tuple[KbStageName, ...]] = ("upload", "extraction", "chunking", "embedding", "publishing")

StageStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED"]
STAGE_PENDING: Final = "PENDING"
STAGE_RUNNING: Final = "RUNNING"
STAGE_COMPLETED: Final = "COMPLETED"
STAGE_FAILED: Final = "FAILED"
STAGE_SKIPPED: Final = "SKIPPED"

DocStatus = Literal["QUEUED", "PROCESSING", "PUBLISHED", "FAILED", "UNPUBLISHED"]
DOC_QUEUED: Final = "QUEUED"
DOC_PROCESSING: Final = "PROCESSING"
DOC_PUBLISHED: Final = "PUBLISHED"
DOC_FAILED: Final = "FAILED"
DOC_UNPUBLISHED: Final = "UNPUBLISHED"

# Per-document vertical added to each selected queue's queue_groups[*].verticals.
KB_VERTICAL_PREFIX: Final = "kbdoc-"
# kb_chunk.chunker_version for imported documents (part of the MERGE natural key).
KB_CHUNKER_VERSION: Final = "docimport-1"
# payload_json.source marker on every imported chunk.
KB_PAYLOAD_SOURCE: Final = "document_import_v1"

ALLOWED_EXTENSIONS: Final[dict[str, str]] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def kb_vertical_for(document_id: int) -> str:
    """Vertical and external_parent_id of an imported document (<= 64 chars)."""
    return f"{KB_VERTICAL_PREFIX}{int(document_id)}"


# ---- Monitoring ----------------------------------------------------------------------------

HealthStatus = Literal["HEALTHY", "FAILED", "NOT_CONFIGURED"]
HEALTH_HEALTHY: Final = "HEALTHY"
HEALTH_FAILED: Final = "FAILED"
HEALTH_NOT_CONFIGURED: Final = "NOT_CONFIGURED"

# (key, label) in display order.
HEALTH_COMPONENTS: Final[tuple[tuple[str, str], ...]] = (
    ("microsoft_graph", "Microsoft connection"),
    ("crm", "CRM connection"),
    ("knowledge_sync", "Knowledge sync"),
    ("extraction", "Extraction service"),
    ("embedding", "Embedding service"),
    ("database", "Database"),
)
HEALTH_COMPONENT_LABELS: Final[dict[str, str]] = dict(HEALTH_COMPONENTS)

# ---- Tables ---------------------------------------------------------------------------------

T_SCHEMA_VERSION: Final = "AIVA_di_schema_version"
T_KB_DOCUMENTS: Final = "AIVA_kb_documents"
T_HEALTH_CHECKS: Final = "AIVA_health_checks"
T_HEALTH_EVENTS: Final = "AIVA_health_check_events"

# Tables this module needs before it can run (migration V001).
REQUIRED_TABLES_V001: Final[tuple[str, ...]] = (T_SCHEMA_VERSION, T_KB_DOCUMENTS, T_HEALTH_CHECKS, T_HEALTH_EVENTS)
SCHEMA_VERSION_V001: Final = "001"

# ---- SharePoint / OneDrive -> CRM (Flow 2, migration V002) ---------------------------------

CrmStageName = Literal["download", "extraction", "intelligence", "entities", "persist"]
CRM_STAGES: Final[tuple[CrmStageName, ...]] = ("download", "extraction", "intelligence", "entities", "persist")

SourceStatus = Literal["ACTIVE", "DISABLED", "DELETED"]
RunTrigger = Literal["SCHEDULED", "MANUAL"]
RunStatus = Literal["QUEUED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED"]
FileState = Literal["ACTIVE", "DELETED"]
FileStatus = Literal["PENDING", "PROCESSING", "COMPLETED", "FAILED", "SKIPPED"]
EntityStatus = Literal["ACTIVE", "WITHDRAWN"]

RUN_QUEUED: Final = "QUEUED"
RUN_RUNNING: Final = "RUNNING"
RUN_COMPLETED: Final = "COMPLETED"
RUN_PARTIAL: Final = "PARTIAL"
RUN_FAILED: Final = "FAILED"

# The schedule presets offered in the UI (days); any 1..365 is accepted as "custom".
SYNC_INTERVAL_PRESETS: Final[tuple[int, ...]] = (7, 14, 21, 28)

T_CRM_SOURCES: Final = "AIVA_crm_sources"
T_CRM_RUNS: Final = "AIVA_crm_sync_runs"
T_CRM_FILES: Final = "AIVA_crm_source_files"
T_CRM_ENTITIES: Final = "AIVA_crm_entities"
REQUIRED_TABLES_V002: Final[tuple[str, ...]] = (T_CRM_SOURCES, T_CRM_RUNS, T_CRM_FILES, T_CRM_ENTITIES)
SCHEMA_VERSION_V002: Final = "002"

# Column byte limits (VARCHAR2 sizes in V001).
MAX_ERROR_BYTES: Final = 4000
MAX_REASON_BYTES: Final = 4000
MAX_ACTION_BYTES: Final = 2000
MAX_FILENAME_CHARS: Final = 255
