"""Async SQL for the SharePoint / OneDrive -> CRM tables (migration V002).

``AIVA_crm_sources``       a folder to sync, its encrypted Microsoft credentials and schedule
``AIVA_crm_sync_runs``     one row per sync. QUEUED rows are the sync work queue; the unique
                           index ``uq_aiva_crm_run_active`` allows one QUEUED/RUNNING run per source
``AIVA_crm_source_files``  the files seen in a source's folder, with the five per-file stages
``AIVA_crm_entities``      the internal CRM store: one row per extracted entity, with provenance

Same conventions as ``kb_repo``: bind variables only, column names only ever from a whitelist,
naive UTC timestamps written (``textutil.utc_now()``) and ISO-8601 + ``Z`` read back
(``textutil.iso_utc``), and text that may hold Arabic cut by bytes (``truncate_utf8``) to fit
its VARCHAR2 column. ``AIVA_accounts`` / ``AIVA_users`` are only read (joins).

Credentials arrive here already encrypted: this module never handles a plaintext secret, and
the secret's ciphertext is selected by ``get_credentials`` only (never by the listing queries).
"""
from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from backend.doc_intel.constants import (
    CRM_STAGES,
    MAX_ERROR_BYTES,
    RUN_FAILED,
    RUN_QUEUED,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    T_CRM_ENTITIES,
    T_CRM_FILES,
    T_CRM_RUNS,
    T_CRM_SOURCES,
)
from backend.doc_intel.kb_repo import NOW, StageChange, dumps_json, loads_json, parse_utc
from backend.doc_intel.queue_config import to_plain
from backend.doc_intel.schemas import (
    CrmEntityOut,
    CrmStageOut,
    DocWarningOut,
    SourceFileOut,
    SourceOut,
    SyncRunOut,
)
from backend.doc_intel.textutil import iso_utc, truncate_utf8, utc_now

if TYPE_CHECKING:
    from backend.database import Database
    from backend.doc_intel.graph_source import RemoteFile, ResolvedTarget

_log = logging.getLogger(__name__)

# ---- statuses (the CHECK constraints of V002) ----------------------------------------------

SOURCE_ACTIVE: Final = "ACTIVE"
SOURCE_DISABLED: Final = "DISABLED"
SOURCE_DELETED: Final = "DELETED"
FILE_ACTIVE: Final = "ACTIVE"
FILE_DELETED: Final = "DELETED"
FILE_PENDING: Final = "PENDING"
FILE_PROCESSING: Final = "PROCESSING"
FILE_COMPLETED: Final = "COMPLETED"
FILE_FAILED: Final = "FAILED"
FILE_SKIPPED: Final = "SKIPPED"
ENTITY_ACTIVE: Final = "ACTIVE"
ENTITY_WITHDRAWN: Final = "WITHDRAWN"
TRIGGER_MANUAL: Final = "MANUAL"
TRIGGER_SCHEDULED: Final = "SCHEDULED"
FINISHED_RUN_STATUSES: Final = ("COMPLETED", "PARTIAL", "FAILED")

# ---- column sizes of V002, in bytes --------------------------------------------------------

MAX_SOURCE_NAME_BYTES: Final = 512
MAX_HINT_BYTES: Final = 16
MAX_SITE_URL_BYTES: Final = 1024
MAX_DRIVE_NAME_BYTES: Final = 512
MAX_FOLDER_PATH_BYTES: Final = 2000
MAX_EXTENSIONS_BYTES: Final = 256
MAX_TENANT_ENC_BYTES: Final = 1024
MAX_CLIENT_ENC_BYTES: Final = 1024
MAX_SECRET_ENC_BYTES: Final = 2048
MAX_RESOLVED_ID_BYTES: Final = 512
MAX_ITEM_ID_BYTES: Final = 512
MAX_FILE_NAME_BYTES: Final = 1024
MAX_FILE_PATH_BYTES: Final = 4000
MAX_URL_BYTES: Final = 4000
MAX_TAG_BYTES: Final = 512
MAX_HASH_BYTES: Final = 128
MAX_ENTITY_TYPE_BYTES: Final = 64
MAX_DISPLAY_BYTES: Final = 1024
MAX_MATCH_KEY_BYTES: Final = 1024
# One file's entities are replaced in one transaction; a pathological document is capped.
MAX_ENTITIES_PER_FILE: Final = 2000

INTERRUPTED_RUN_REASON: Final = "Interrupted (server restart or crash) — use Sync now"
SHUTDOWN_RUN_REASON: Final = "Interrupted by shutdown — use Sync now"
INTERRUPTED_FILE_REASON: Final = "Interrupted before the file finished — it is retried on the next sync"
SOURCE_DELETED_REASON: Final = "The source was deleted"

_CLAIM_ATTEMPTS: Final = 3
_IN_CHUNK: Final = 500  # ids per "IN (...)" list (Oracle allows 1000)
_ACTIVE_RUN_INDEX: Final = "UQ_AIVA_CRM_RUN_ACTIVE"

_FILE_STAGE_COLUMNS: Final[dict[str, str]] = {stage: f"{stage}_status" for stage in CRM_STAGES}

_SOURCE_WRITABLE: Final = frozenset(
    {
        "name",
        "account_id",
        "tenant_id_enc",
        "client_id_enc",
        "client_secret_enc",
        "client_secret_hint",
        "secret_updated_at",
        "site_url",
        "drive_name",
        "folder_path",
        "recursive",
        "file_extensions",
        "use_intelligence",
        "resolved_site_id",
        "resolved_drive_id",
        "resolved_folder_id",
        "sync_enabled",
        "sync_interval_days",
        "sync_hour",
        "next_sync_at",
        "status",
    }
)
_FILE_WRITABLE: Final = frozenset(
    {
        "name",
        "path",
        "web_url",
        "etag",
        "ctag",
        "quick_xor_hash",
        "content_sha256",
        "size_bytes",
        "modified_at",
        "state",
        "status",
        "failed_stage",
        "error_message",
        "warnings_json",
        "result_json",
        "entity_count",
        "is_valid",
        "attempts",
        "last_run_id",
        "last_seen_at",
        "processed_at",
        "deleted_at",
    }
)
_RUN_COUNT_COLUMNS: Final = (
    "files_seen",
    "files_new",
    "files_changed",
    "files_deleted",
    "files_unchanged",
    "files_failed",
)


class _Increment:
    """Column value placeholder: "the current value + 1"."""


INCREMENT = _Increment()


class RunAlreadyActive(Exception):
    """A QUEUED or RUNNING run already exists for the source (unique index uq_aiva_crm_run_active)."""

    def __init__(self, source_id: int) -> None:
        super().__init__(f"A sync is already queued or running for source {source_id}")
        self.source_id = source_id


# ---- pure helpers ----------------------------------------------------------------------------


def file_stage_column(stage: str) -> str:
    try:
        return _FILE_STAGE_COLUMNS[stage]
    except KeyError:
        raise ValueError(f"Unknown stage: {stage!r}") from None


def apply_file_stage_change(details: Mapping[str, Any] | None, change: StageChange, *, now: datetime) -> dict[str, Any]:
    """New ``stage_details`` of a source file after ``change``.

    The rules of ``kb_repo.apply_stage_change``, for the SharePoint stages: RUNNING starts a
    fresh entry; COMPLETED/FAILED stamp ``finished_at``, ``seconds``, ``metrics`` (and
    ``error``); PENDING/SKIPPED drop the entry.
    """
    file_stage_column(change.stage)
    out: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in (details or {}).items()}
    stamp = iso_utc(now)
    if change.status == STAGE_RUNNING:
        out[change.stage] = {"started_at": iso_utc(change.started_at) if change.started_at else stamp}
    elif change.status in (STAGE_COMPLETED, STAGE_FAILED):
        entry = dict(out.get(change.stage) or {})
        if change.started_at is not None:
            entry["started_at"] = iso_utc(change.started_at)
        entry.setdefault("started_at", stamp)
        entry["finished_at"] = stamp
        begun = parse_utc(entry.get("started_at"))
        if begun is not None:
            entry["seconds"] = round(max(0.0, (now - begun).total_seconds()), 3)
        if change.metrics:
            entry["metrics"] = to_plain(dict(change.metrics))
        if change.status == STAGE_FAILED:
            entry["error"] = truncate_utf8(change.error or "Failed", MAX_ERROR_BYTES)
        else:
            entry.pop("error", None)
        out[change.stage] = entry
    elif change.status in (STAGE_PENDING, STAGE_SKIPPED):
        out.pop(change.stage, None)
    else:
        raise ValueError(f"Unknown stage status: {change.status!r}")
    return out


def running_file_stage(row: Mapping[str, Any]) -> str:
    """The stage to mark FAILED for an interrupted PROCESSING file."""
    for stage in CRM_STAGES:
        if row.get(file_stage_column(stage)) == STAGE_RUNNING:
            return stage
    for stage in CRM_STAGES:
        if row.get(file_stage_column(stage)) in (STAGE_PENDING, STAGE_FAILED):
            return stage
    return CRM_STAGES[-1]


def extensions_from_column(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def fits(text: str | None, max_bytes: int) -> bool:
    return text is None or len(text.encode("utf-8")) <= max_bytes


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _float(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_active_run_violation(ex: BaseException) -> bool:
    text = str(ex).upper()
    return "ORA-00001" in text and _ACTIVE_RUN_INDEX in text


def _in_list(prefix: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(f":{name}" for name in binds), binds


def _chunks(values: Sequence[Any], size: int = _IN_CHUNK) -> list[Sequence[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def _https_url(url: Any) -> str | None:
    """A link the UI may render: https only (never javascript:/data:)."""
    text = str(url or "").strip()
    return text if text.lower().startswith("https://") else None


# ---- CRM entities: display value and match key -------------------------------------------------

META_KEY: Final = "_meta"
# display_field of crm-document-ingestion's generic starter schemas: the fallback when the
# extraction result does not describe its schemas. The library itself is not imported here
# (it pulls in document-extractor, which only ever runs in the extraction child process).
GENERIC_DISPLAY_FIELDS: Final[dict[str, str]] = {
    "organization": "name",
    "contact": "full_name",
    "document_reference": "reference_number",
}
_NAME_FIELDS: Final = ("name", "full_name", "display_name", "company_name", "title", "reference_number")


def schema_hints(metrics: Mapping[str, Any] | None) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """(entity type -> display field, entity type -> {field: type}) from the extraction result's
    ``metrics["schemas"]`` (generic + client schemas, as the child registered them), over the
    generic defaults."""
    display = dict(GENERIC_DISPLAY_FIELDS)
    types: dict[str, dict[str, str]] = {}
    schemas = (metrics or {}).get("schemas")
    if isinstance(schemas, Mapping):
        for name, info in schemas.items():
            if not isinstance(name, str) or not isinstance(info, Mapping):
                continue
            if isinstance(info.get("display_field"), str) and info["display_field"]:
                display[name] = info["display_field"]
            field_types = info.get("field_types")
            if isinstance(field_types, Mapping):
                types[name] = {str(k): str(v) for k, v in field_types.items()}
    return display, types


def _scalar_text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _scalar_text(item)
            if text:
                return text
    return None


def _field_value(
    record: Mapping[str, Any],
    name: str,
    contains: tuple[str, ...],
    *,
    field_types: Mapping[str, str] | None = None,
    field_type: str | None = None,
) -> str | None:
    """A field typed ``field_type`` by the schema, else the record's ``name`` field, else the
    first field whose name contains one of ``contains``."""
    if field_types and field_type:
        for key, kind in field_types.items():
            if kind == field_type:
                text = _scalar_text(record.get(key))
                if text:
                    return text
    exact = _scalar_text(record.get(name))
    if exact:
        return exact
    for key, value in record.items():
        if key == META_KEY or not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(token in lowered for token in contains):
            text = _scalar_text(value)
            if text:
                return text
    return None


def _ascii_digits(text: str) -> str:
    """Every decimal digit (Arabic-Indic included) as an ASCII digit; everything else dropped."""
    return "".join(str(unicodedata.decimal(ch)) for ch in text if ch.isdecimal())


def entity_display_value(record: Mapping[str, Any], entity_type: str, display_fields: Mapping[str, str]) -> str | None:
    """The schema's display field (``_meta.display_field`` when the extractor provides one),
    else a name-like field, else the first scalar field."""
    meta = record.get(META_KEY) if isinstance(record.get(META_KEY), dict) else {}
    candidates: list[str] = []
    for key in (meta.get("display_field"), display_fields.get(entity_type)):
        if isinstance(key, str) and key and key not in candidates:
            candidates.append(key)
    candidates += [k for k in _NAME_FIELDS if k not in candidates]
    for key in candidates:
        text = _scalar_text(record.get(key))
        if text:
            return truncate_utf8(text, MAX_DISPLAY_BYTES)
    for key, value in record.items():
        if key != META_KEY:
            text = _scalar_text(value)
            if text:
                return truncate_utf8(text, MAX_DISPLAY_BYTES)
    return None


def entity_match_key(
    record: Mapping[str, Any], display_value: str | None, field_types: Mapping[str, str] | None = None
) -> str | None:
    """Normalized key for matching the same real-world entity across files, in this order:
    lower-cased email, digits-only phone, tax id, case-folded name (display value).
    ``field_types`` (the schema's field -> type) finds email/phone fields by type first."""
    email = _field_value(record, "email", ("email", "e_mail", "mail"), field_types=field_types, field_type="email")
    if email and "@" in email:
        return truncate_utf8("email:" + email.strip().lower(), MAX_MATCH_KEY_BYTES)
    phone = _field_value(record, "phone", ("phone", "mobile", "tel"), field_types=field_types, field_type="phone")
    if phone:
        digits = _ascii_digits(phone)
        if len(digits) >= 6:
            return truncate_utf8("phone:" + digits, MAX_MATCH_KEY_BYTES)
    tax = _field_value(record, "tax_id", ("tax", "vat"))
    if tax:
        key = re.sub(r"[\s\-./]", "", tax).upper()
        if key:
            return truncate_utf8("tax_id:" + key, MAX_MATCH_KEY_BYTES)
    if display_value:
        folded = " ".join(display_value.split()).casefold()
        if folded:
            return truncate_utf8("name:" + folded, MAX_MATCH_KEY_BYTES)
    return None


@dataclass(frozen=True)
class EntityRow:
    """One CRM-store row built from an extracted entity (``to_crm_json()`` shape)."""

    entity_type: str
    display_value: str | None
    match_key: str | None
    confidence: float | None
    is_valid: bool | None
    # The whole to_crm_json() entity: record fields plus the "_meta" provenance block.
    fields: dict[str, Any] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)


def build_entity_rows(
    entities: Sequence[Mapping[str, Any]],
    issues: Sequence[Mapping[str, Any]] = (),
    *,
    display_fields: Mapping[str, str] | None = None,
    field_types: Mapping[str, Mapping[str, str]] | None = None,
) -> list[EntityRow]:
    """Store rows for one file's entities. ``issues`` are the validation issues
    (``entity_index`` = position in ``entities``); an entity with an error issue is invalid.
    ``display_fields`` / ``field_types`` come from ``schema_hints`` (generic defaults otherwise)."""
    names = display_fields if display_fields is not None else GENERIC_DISPLAY_FIELDS
    types = field_types or {}
    by_index: dict[int, list[dict[str, Any]]] = {}
    for issue in issues:
        if isinstance(issue, Mapping) and isinstance(issue.get("entity_index"), int):
            by_index.setdefault(int(issue["entity_index"]), []).append(to_plain(dict(issue)))
    rows: list[EntityRow] = []
    for index, raw in enumerate(entities[:MAX_ENTITIES_PER_FILE]):
        if not isinstance(raw, Mapping):
            continue
        record = to_plain(dict(raw))
        meta = record.get(META_KEY) if isinstance(record.get(META_KEY), dict) else {}
        entity_type = str(meta.get("entity_type") or record.get("entity_type") or "unknown").strip() or "unknown"
        entity_type = truncate_utf8(entity_type, MAX_ENTITY_TYPE_BYTES) or "unknown"
        display = entity_display_value(record, entity_type, names)
        confidence = _float(meta.get("confidence"))
        if confidence is not None:
            confidence = min(1.0, max(0.0, confidence))
        own = by_index.get(index, [])
        rows.append(
            EntityRow(
                entity_type=entity_type,
                display_value=display,
                match_key=entity_match_key(record, display, types.get(entity_type)),
                confidence=confidence,
                is_valid=not any(str(i.get("severity")) == "error" for i in own),
                fields=record,
                issues=own,
            )
        )
    return rows


# ---- row -> API model -------------------------------------------------------------------------

_EMPTY_COUNTS: Final = {"files_active": 0, "files_failed": 0, "files_deleted": 0, "entities_active": 0}


def run_to_out(row: Mapping[str, Any]) -> SyncRunOut:
    return SyncRunOut(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        trigger_type=str(row.get("trigger_type") or TRIGGER_MANUAL),
        triggered_by=_int(row.get("triggered_by")),
        triggered_by_email=row.get("triggered_by_email"),
        status=str(row.get("status") or RUN_QUEUED),
        files_seen=_int(row.get("files_seen")) or 0,
        files_new=_int(row.get("files_new")) or 0,
        files_changed=_int(row.get("files_changed")) or 0,
        files_deleted=_int(row.get("files_deleted")) or 0,
        files_unchanged=_int(row.get("files_unchanged")) or 0,
        files_failed=_int(row.get("files_failed")) or 0,
        error_message=row.get("error_message"),
        details=loads_json(row.get("details_json"), {}),
        created_at=iso_utc(row.get("created_at")),
        started_at=iso_utc(row.get("started_at")),
        finished_at=iso_utc(row.get("finished_at")),
    )


def source_to_out(
    row: Mapping[str, Any],
    *,
    box: Any = None,
    active_run: Mapping[str, Any] | None = None,
    counts: Mapping[str, int] | None = None,
    show_secret_hint: bool = True,
) -> SourceOut:
    """API model of a source. Tenant and client IDs are decrypted with ``box``; without a
    usable key (None, or a decrypt failure) they are omitted and ``credentials_readable`` is
    False. The client secret is never decrypted here, and its hint is only shown when
    ``show_secret_hint`` (Super Admin views)."""
    tenant_id = client_id = None
    readable = True
    if row.get("tenant_id_enc") or row.get("client_id_enc"):
        if box is None:
            readable = False
        else:
            try:
                tenant_id = box.decrypt(row["tenant_id_enc"]) if row.get("tenant_id_enc") else None
                client_id = box.decrypt(row["client_id_enc"]) if row.get("client_id_enc") else None
            except Exception:  # SecretsUnavailable (key rotated away, tampered text): the list still works
                tenant_id = client_id = None
                readable = False
    status = str(row.get("status") or SOURCE_ACTIVE)
    return SourceOut(
        id=int(row["id"]),
        name=str(row.get("name") or ""),
        account_id=_int(row.get("account_id")),
        account_name=row.get("account_name"),
        provider=str(row.get("provider") or "microsoft_graph"),
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret_set=bool(_int(row.get("client_secret_set"))),
        client_secret_hint=row.get("client_secret_hint") if show_secret_hint else None,
        secret_updated_at=iso_utc(row.get("secret_updated_at")),
        credentials_readable=readable,
        site_url=row.get("site_url"),
        drive_name=row.get("drive_name"),
        folder_path=row.get("folder_path"),
        recursive=bool(_int(row.get("recursive")) if row.get("recursive") is not None else True),
        file_extensions=extensions_from_column(row.get("file_extensions")),
        use_intelligence=bool(_int(row.get("use_intelligence")) or 0),
        sync_enabled=bool(_int(row.get("sync_enabled")) or 0),
        sync_interval_days=_int(row.get("sync_interval_days")) or 14,
        sync_hour=_int(row.get("sync_hour")) if _int(row.get("sync_hour")) is not None else 2,
        next_sync_at=iso_utc(row.get("next_sync_at")),
        last_sync_at=iso_utc(row.get("last_sync_at")),
        last_sync_status=row.get("last_sync_status") or None,
        last_sync_error=row.get("last_sync_error"),
        last_success_at=iso_utc(row.get("last_success_at")),
        status=status,
        active_run=run_to_out(active_run) if active_run else None,
        counts={**_EMPTY_COUNTS, **dict(counts or {})},
        created_at=iso_utc(row.get("created_at")),
        updated_at=iso_utc(row.get("updated_at")),
    )


def _warnings(value: Any) -> list[DocWarningOut]:
    out: list[DocWarningOut] = []
    for item in loads_json(value, []):
        if isinstance(item, dict) and item.get("message"):
            page = item.get("page")
            out.append(
                DocWarningOut(
                    code=str(item.get("code") or "warning"),
                    message=str(item.get("message")),
                    page=int(page) if isinstance(page, (int, float)) and not isinstance(page, bool) else None,
                )
            )
    return out


def file_to_out(row: Mapping[str, Any]) -> SourceFileOut:
    """API model of a tracked file: always the five stages, in pipeline order."""
    details = loads_json(row.get("stage_details"), {})
    failed_stage = row.get("failed_stage") if row.get("failed_stage") in CRM_STAGES else None
    stages: list[CrmStageOut] = []
    for stage in CRM_STAGES:
        status = str(row.get(file_stage_column(stage)) or STAGE_PENDING)
        entry = details.get(stage) if isinstance(details.get(stage), dict) else {}
        error = entry.get("error") if status == STAGE_FAILED else None
        if status == STAGE_FAILED and not error and failed_stage == stage:
            error = row.get("error_message")
        stages.append(
            CrmStageOut(
                name=stage,
                status=status,
                error=error,
                started_at=iso_utc(entry.get("started_at")),
                finished_at=iso_utc(entry.get("finished_at")),
            )
        )
    return SourceFileOut(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        name=row.get("name"),
        path=row.get("path"),
        web_url=_https_url(row.get("web_url")),
        size_bytes=_int(row.get("size_bytes")),
        modified_at=iso_utc(row.get("modified_at")),
        state=str(row.get("state") or FILE_ACTIVE),
        status=str(row.get("status") or FILE_PENDING),
        stages=stages,
        failed_stage=failed_stage,
        error_message=row.get("error_message"),
        warnings=_warnings(row.get("warnings_json")),
        entity_count=_int(row.get("entity_count")),
        is_valid=_bool(row.get("is_valid")),
        attempts=_int(row.get("attempts")) or 0,
        last_run_id=_int(row.get("last_run_id")),
        first_seen_at=iso_utc(row.get("first_seen_at")),
        last_seen_at=iso_utc(row.get("last_seen_at")),
        processed_at=iso_utc(row.get("processed_at")),
        deleted_at=iso_utc(row.get("deleted_at")),
        updated_at=iso_utc(row.get("updated_at")),
    )


def entity_to_out(row: Mapping[str, Any]) -> CrmEntityOut:
    """API model of a CRM-store row: ``fields`` is the record, ``provenance`` its "_meta" block.
    An entity whose source was deleted stays listed, its source named "<name> (deleted)"."""
    stored = loads_json(row.get("fields_json"), {})
    meta = stored.get(META_KEY) if isinstance(stored.get(META_KEY), dict) else {}
    source_name = row.get("source_name")
    if source_name and row.get("source_status") == SOURCE_DELETED:
        source_name = f"{source_name} (deleted)"
    return CrmEntityOut(
        id=int(row["id"]),
        source_id=int(row["source_id"]),
        source_name=source_name,
        source_file_id=int(row["source_file_id"]),
        file_name=row.get("file_name"),
        account_id=_int(row.get("account_id")),
        entity_type=str(row.get("entity_type") or "unknown"),
        display_value=row.get("display_value"),
        confidence=_float(row.get("confidence")),
        is_valid=_bool(row.get("is_valid")),
        fields={k: v for k, v in stored.items() if k != META_KEY},
        provenance=meta,
        issues=[i for i in loads_json(row.get("issues_json"), []) if isinstance(i, dict)],
        status=str(row.get("status") or ENTITY_ACTIVE),
        created_at=iso_utc(row.get("created_at")),
        updated_at=iso_utc(row.get("updated_at")),
        withdrawn_at=iso_utc(row.get("withdrawn_at")),
    )


# ---- SQL -------------------------------------------------------------------------------------------

_SOURCE_SELECT = f"""
    SELECT s.id, s.name, s.account_id, s.provider, s.tenant_id_enc, s.client_id_enc,
           CASE WHEN s.client_secret_enc IS NULL THEN 0 ELSE 1 END AS client_secret_set,
           s.client_secret_hint, s.secret_updated_at, s.site_url, s.drive_name, s.folder_path,
           s.recursive, s.file_extensions, s.use_intelligence, s.resolved_site_id, s.resolved_drive_id,
           s.resolved_folder_id, s.sync_enabled, s.sync_interval_days, s.sync_hour, s.next_sync_at,
           s.last_sync_at, s.last_sync_status, s.last_sync_error, s.last_success_at, s.status,
           s.created_by, s.updated_by, s.created_at, s.updated_at, a.name AS account_name
    FROM {T_CRM_SOURCES} s
    LEFT JOIN AIVA_accounts a ON a.id = s.account_id
"""

_RUN_SELECT = f"""
    SELECT r.id, r.source_id, r.trigger_type, r.triggered_by, r.status, r.worker_id, r.files_seen,
           r.files_new, r.files_changed, r.files_deleted, r.files_unchanged, r.files_failed,
           r.error_message, r.details_json, r.created_at, r.started_at, r.finished_at, r.updated_at,
           u.email AS triggered_by_email
    FROM {T_CRM_RUNS} r
    LEFT JOIN AIVA_users u ON u.id = r.triggered_by
"""

# result_json (the full extraction result, possibly large) is deliberately not listed.
_FILE_SELECT = f"""
    SELECT f.id, f.source_id, f.drive_id, f.item_id, f.name, f.path, f.web_url, f.etag, f.ctag,
           f.quick_xor_hash, f.content_sha256, f.size_bytes, f.modified_at, f.state, f.status,
           f.download_status, f.extraction_status, f.intelligence_status, f.entities_status,
           f.persist_status, f.failed_stage, f.error_message, f.warnings_json, f.stage_details,
           f.entity_count, f.is_valid, f.attempts, f.last_run_id, f.first_seen_at, f.last_seen_at,
           f.processed_at, f.deleted_at, f.updated_at
    FROM {T_CRM_FILES} f
"""

_ENTITY_SELECT = f"""
    SELECT e.id, e.source_id, e.source_file_id, e.account_id, e.entity_type, e.display_value,
           e.match_key, e.confidence, e.is_valid, e.fields_json, e.issues_json, e.status,
           e.created_at, e.updated_at, e.withdrawn_at,
           s.name AS source_name, s.status AS source_status, f.name AS file_name
    FROM {T_CRM_ENTITIES} e
    LEFT JOIN {T_CRM_SOURCES} s ON s.id = e.source_id
    LEFT JOIN {T_CRM_FILES} f ON f.id = e.source_file_id
"""

_ENTITY_INSERT = f"""
    INSERT INTO {T_CRM_ENTITIES} (
        source_id, source_file_id, account_id, entity_type, display_value, match_key, confidence,
        is_valid, fields_json, issues_json, status, created_at, updated_at
    ) VALUES (
        :source_id, :source_file_id, :account_id, :entity_type, :display_value, :match_key, :confidence,
        :is_valid, :fields_json, :issues_json, 'ACTIVE', :now, :now
    )
"""


def remote_columns(remote: RemoteFile) -> dict[str, Any]:
    """Listing metadata of a remote file, cut to its column sizes."""
    web_url = remote.web_url if remote.web_url and fits(remote.web_url, MAX_URL_BYTES) else None
    return {
        "name": truncate_utf8(remote.name, MAX_FILE_NAME_BYTES),
        "path": truncate_utf8(remote.path, MAX_FILE_PATH_BYTES),
        "web_url": web_url,
        "etag": truncate_utf8(remote.etag, MAX_TAG_BYTES),
        "ctag": truncate_utf8(remote.ctag, MAX_TAG_BYTES),
        "quick_xor_hash": truncate_utf8(remote.quick_xor_hash, MAX_HASH_BYTES),
        "size_bytes": remote.size,
        "modified_at": remote.modified_at,
    }


class CrmRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- accounts (read-only) -----------------------------------------------------------------

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT id, name FROM AIVA_accounts WHERE id = :id", {"id": int(account_id)})

    # ---- sources ------------------------------------------------------------------------------------

    async def insert_source(
        self,
        *,
        name: str,
        account_id: int | None,
        tenant_id_enc: str,
        client_id_enc: str,
        client_secret_enc: str,
        client_secret_hint: str | None,
        secret_updated_at: datetime,
        site_url: str,
        drive_name: str | None,
        folder_path: str | None,
        recursive: bool,
        file_extensions: Sequence[str],
        sync_enabled: bool,
        sync_interval_days: int,
        sync_hour: int,
        next_sync_at: datetime | None,
        created_by: int | None,
        use_intelligence: bool = False,
    ) -> int:
        now = utc_now()
        source_id = await self._db.execute(
            f"""
            INSERT INTO {T_CRM_SOURCES} (
                name, account_id, provider, tenant_id_enc, client_id_enc, client_secret_enc,
                client_secret_hint, secret_updated_at, site_url, drive_name, folder_path, recursive,
                file_extensions, use_intelligence, sync_enabled, sync_interval_days, sync_hour,
                next_sync_at, status, created_by, updated_by, created_at, updated_at
            ) VALUES (
                :name, :account_id, 'microsoft_graph', :tenant_id_enc, :client_id_enc, :client_secret_enc,
                :client_secret_hint, :secret_updated_at, :site_url, :drive_name, :folder_path, :recursive,
                :file_extensions, :use_intelligence, :sync_enabled, :sync_interval_days, :sync_hour,
                :next_sync_at, 'ACTIVE', :created_by, :created_by, :now, :now
            ) RETURNING id INTO :out_id
            """,
            {
                "name": truncate_utf8(name, MAX_SOURCE_NAME_BYTES),
                "account_id": account_id,
                "tenant_id_enc": tenant_id_enc,
                "client_id_enc": client_id_enc,
                "client_secret_enc": client_secret_enc,
                "client_secret_hint": client_secret_hint,
                "secret_updated_at": secret_updated_at,
                "site_url": site_url,
                "drive_name": drive_name,
                "folder_path": folder_path,
                "recursive": int(bool(recursive)),
                "file_extensions": ",".join(file_extensions),
                "use_intelligence": int(bool(use_intelligence)),
                "sync_enabled": int(bool(sync_enabled)),
                "sync_interval_days": int(sync_interval_days),
                "sync_hour": int(sync_hour),
                "next_sync_at": next_sync_at,
                "created_by": created_by,
                "now": now,
            },
            return_id=True,
        )
        assert source_id is not None
        return int(source_id)

    async def get_source(self, source_id: int, *, include_deleted: bool = False) -> dict[str, Any] | None:
        where = "WHERE s.id = :id" + ("" if include_deleted else " AND s.status <> 'DELETED'")
        return await self._db.fetch_one(f"{_SOURCE_SELECT} {where}", {"id": int(source_id)})

    async def list_sources(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        where = "" if include_deleted else "WHERE s.status <> 'DELETED'"
        return await self._db.fetch_all(f"{_SOURCE_SELECT} {where} ORDER BY s.created_at, s.id")

    async def get_credentials(self, source_id: int) -> dict[str, Any] | None:
        """The three ciphertexts of a live source (the only query that reads the secret's)."""
        return await self._db.fetch_one(
            f"""
            SELECT tenant_id_enc, client_id_enc, client_secret_enc FROM {T_CRM_SOURCES}
            WHERE id = :id AND status <> 'DELETED'
            """,
            {"id": int(source_id)},
        )

    async def update_source(self, source_id: int, columns: Mapping[str, Any], *, updated_by: int | None) -> bool:
        """An admin change (stamps updated_at / updated_by); False when the source is gone or deleted."""
        return await self._update_source(source_id, columns, updated_by=updated_by, admin=True)

    async def set_resolved(
        self,
        source_id: int,
        target: ResolvedTarget,
        *,
        site_url: str | None,
        drive_name: str | None,
        folder_path: str | None,
    ) -> bool:
        """Cache the resolved Graph ids, unless the location changed meanwhile (the admin edit
        cleared them). DECODE treats two NULLs as equal, so the unset library/folder match."""
        values = {
            "site_id": target.site_id,
            "drive_id": target.drive_id,
            "folder_id": target.folder_id,
        }
        if not all(fits(v, MAX_RESOLVED_ID_BYTES) for v in values.values()):
            return False
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"""
                UPDATE {T_CRM_SOURCES}
                SET resolved_site_id = :site_id, resolved_drive_id = :drive_id, resolved_folder_id = :folder_id
                WHERE id = :id AND status <> 'DELETED'
                  AND DECODE(site_url, :site_url, 1, 0) = 1
                  AND DECODE(drive_name, :drive_name, 1, 0) = 1
                  AND DECODE(folder_path, :folder_path, 1, 0) = 1
                """,
                {**values, "id": int(source_id), "site_url": site_url, "drive_name": drive_name, "folder_path": folder_path},
            )
            return cur.rowcount == 1

    async def soft_delete_source(self, source_id: int, *, updated_by: int | None) -> int | None:
        """Soft delete, in one transaction: the source becomes DELETED (its runs and files are
        kept as history), its schedule stops, its stored secret is wiped and its ACTIVE entities
        are WITHDRAWN. Returns how many entities were withdrawn; None when the source is gone or
        already deleted. Raises RunAlreadyActive while a run is QUEUED or RUNNING: the source row
        is locked first, so the scheduler cannot claim it in the meantime."""
        now = utc_now()
        source_id = int(source_id)
        async with self._db.connection() as conn:
            row = await self._db.fetch_one(
                f"SELECT status FROM {T_CRM_SOURCES} WHERE id = :id FOR UPDATE", {"id": source_id}, conn=conn
            )
            if row is None or row.get("status") == SOURCE_DELETED:
                return None
            active = await self._db.fetch_one(
                f"SELECT COUNT(*) AS n FROM {T_CRM_RUNS} WHERE source_id = :id AND status IN ('QUEUED', 'RUNNING')",
                {"id": source_id},
                conn=conn,
            )
            if int((active or {}).get("n") or 0):
                raise RunAlreadyActive(source_id)
            cur = conn.cursor()
            await cur.execute(
                f"""
                UPDATE {T_CRM_SOURCES}
                SET status = 'DELETED', sync_enabled = 0, next_sync_at = NULL, client_secret_enc = NULL,
                    client_secret_hint = NULL, updated_by = :updated_by, updated_at = :now
                WHERE id = :id AND status <> 'DELETED'
                """,
                {"id": source_id, "updated_by": updated_by, "now": now},
            )
            if cur.rowcount != 1:
                return None
            withdraw = conn.cursor()
            await withdraw.execute(
                f"""
                UPDATE {T_CRM_ENTITIES} SET status = 'WITHDRAWN', withdrawn_at = :now, updated_at = :now
                WHERE source_id = :id AND status = 'ACTIVE'
                """,
                {"id": source_id, "now": now},
            )
            return max(0, int(withdraw.rowcount or 0))

    async def due_sources(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        """ACTIVE sources whose schedule is on and due (read-only)."""
        return await self._db.fetch_all(
            f"""
            SELECT id, name, next_sync_at, sync_interval_days, sync_hour FROM {T_CRM_SOURCES}
            WHERE status = 'ACTIVE' AND sync_enabled = 1 AND next_sync_at IS NOT NULL AND next_sync_at <= :now
            ORDER BY next_sync_at, id
            FETCH FIRST :limit ROWS ONLY
            """,
            {"now": now, "limit": max(1, int(limit))},
        )

    async def claim_due_source(self, source_id: int, *, now: datetime, new: datetime) -> bool:
        """Optimistic claim of a due schedule: moves next_sync_at to ``new`` (after ``now``) only
        while the schedule is on and still due at ``now``. The first claimer moves it past
        ``now``, so a second process or replica finds nothing due and gets rowcount 0.

        Deliberately "still due" and not ``next_sync_at = :old``: python-oracledb binds a
        datetime as DATE, which drops fractional seconds, so a stored value that has them
        (written by SQL, e.g. SYSTIMESTAMP, or by hand) would never compare equal and the
        source would never be claimed again (review finding F24)."""
        if new <= now:
            raise ValueError("claim_due_source() needs a new next_sync_at after now")
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"""
                UPDATE {T_CRM_SOURCES} SET next_sync_at = :new
                WHERE id = :id AND status = 'ACTIVE' AND sync_enabled = 1
                  AND next_sync_at IS NOT NULL AND next_sync_at <= :now
                """,
                {"id": int(source_id), "now": now, "new": new},
            )
            return cur.rowcount == 1

    async def restore_next_sync(self, source_id: int, *, expected: datetime, value: datetime) -> bool:
        """Undo a claim whose run could not be queued (only if the claim is still in place).

        ``expected`` is the ``new`` value this process's claim wrote. It goes through the same
        DATE bind as that write, so the equality holds even when ``new`` had fractional seconds."""
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"UPDATE {T_CRM_SOURCES} SET next_sync_at = :value WHERE id = :id AND next_sync_at = :expected",
                {"id": int(source_id), "expected": expected, "value": value},
            )
            return cur.rowcount == 1

    # ---- runs -----------------------------------------------------------------------------------------

    async def create_run(self, source_id: int, *, trigger_type: str, triggered_by: int | None) -> int:
        """Insert a QUEUED run. Raises RunAlreadyActive when one is already QUEUED or RUNNING."""
        now = utc_now()
        try:
            run_id = await self._db.execute(
                f"""
                INSERT INTO {T_CRM_RUNS} (source_id, trigger_type, triggered_by, status, created_at, updated_at)
                VALUES (:source_id, :trigger_type, :triggered_by, 'QUEUED', :now, :now)
                RETURNING id INTO :out_id
                """,
                {"source_id": int(source_id), "trigger_type": trigger_type, "triggered_by": triggered_by, "now": now},
                return_id=True,
            )
        except Exception as ex:
            if _is_active_run_violation(ex):
                raise RunAlreadyActive(int(source_id)) from None
            raise
        assert run_id is not None
        return int(run_id)

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(f"{_RUN_SELECT} WHERE r.id = :id", {"id": int(run_id)})

    async def list_runs(self, source_id: int, *, limit: int = 20, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        rows = await self._db.fetch_all(
            f"""
            {_RUN_SELECT} WHERE r.source_id = :source_id
            ORDER BY r.created_at DESC, r.id DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
            """,
            {"source_id": int(source_id), "offset": max(0, int(offset)), "limit": max(1, int(limit))},
        )
        total = await self._db.fetch_one(
            f"SELECT COUNT(*) AS total FROM {T_CRM_RUNS} WHERE source_id = :source_id", {"source_id": int(source_id)}
        )
        return rows, int((total or {}).get("total") or 0)

    async def active_runs(self, source_id: int | None = None) -> dict[int, dict[str, Any]]:
        """The QUEUED/RUNNING run of each source (at most one, by the unique index)."""
        sql = f"{_RUN_SELECT} WHERE r.status IN ('QUEUED', 'RUNNING')"
        binds: dict[str, Any] = {}
        if source_id is not None:
            sql += " AND r.source_id = :source_id"
            binds["source_id"] = int(source_id)
        rows = await self._db.fetch_all(sql, binds)
        return {int(r["source_id"]): r for r in rows}

    async def claim_next_run(self, worker_id: str) -> dict[str, Any] | None:
        """Claim the oldest QUEUED run (conditional UPDATE, safe across processes)."""
        for _ in range(_CLAIM_ATTEMPTS):
            row = await self._db.fetch_one(
                f"SELECT id FROM {T_CRM_RUNS} WHERE status = 'QUEUED' ORDER BY created_at, id FETCH FIRST 1 ROWS ONLY"
            )
            if row is None:
                return None
            run_id = int(row["id"])
            async with self._db.connection() as conn:
                cur = conn.cursor()
                now = utc_now()
                await cur.execute(
                    f"""
                    UPDATE {T_CRM_RUNS}
                    SET status = 'RUNNING', worker_id = :worker_id, started_at = :now, updated_at = :now
                    WHERE id = :id AND status = 'QUEUED'
                    """,
                    {"worker_id": worker_id[:128], "now": now, "id": run_id},
                )
                claimed = cur.rowcount == 1
            if claimed:
                return await self.get_run(run_id)
        return None

    async def touch_run(self, run_id: int, worker_id: str) -> None:
        """Heartbeat: keep a RUNNING run's updated_at fresh (only this worker's)."""
        await self._db.execute(
            f"UPDATE {T_CRM_RUNS} SET updated_at = :now WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id",
            {"now": utc_now(), "id": int(run_id), "worker_id": worker_id[:128]},
        )

    async def set_run_progress(
        self,
        run_id: int,
        *,
        worker_id: str,
        counts: Mapping[str, int],
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """Live counters of a RUNNING run (and its details), for the UI."""
        sets: list[str] = []
        binds: dict[str, Any] = {"id": int(run_id), "worker_id": worker_id[:128], "now": utc_now()}
        for name in _RUN_COUNT_COLUMNS:
            if name in counts:
                sets.append(f"{name} = :{name}")
                binds[name] = int(counts[name])
        if details is not None:
            sets.append("details_json = :details_json")
            binds["details_json"] = dumps_json(dict(details))
        sets.append("updated_at = :now")
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"UPDATE {T_CRM_RUNS} SET {', '.join(sets)} WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id",
                binds,
            )
            return cur.rowcount == 1

    async def finish_run(
        self,
        run_id: int,
        *,
        worker_id: str,
        status: str,
        error_message: str | None,
        counts: Mapping[str, int],
        details: Mapping[str, Any],
        source_id: int,
        next_sync_at: datetime | None,
        schedule_interval_days: int | None,
        schedule_hour: int | None,
    ) -> bool:
        """End a RUNNING run and record it on its source, in one transaction.

        ``next_sync_at`` (computed for ``schedule_interval_days`` / ``schedule_hour``) is only
        written while the source's schedule is on and still has those values, so an admin edit
        made during the run wins. False (and nothing written) when the run is no longer this
        worker's RUNNING run (recovered as interrupted meanwhile).
        """
        if status not in FINISHED_RUN_STATUSES:
            raise ValueError(f"finish_run() takes COMPLETED, PARTIAL or FAILED, not {status!r}")
        now = utc_now()
        error = truncate_utf8(error_message, MAX_ERROR_BYTES)
        binds: dict[str, Any] = {
            "id": int(run_id),
            "worker_id": worker_id[:128],
            "status": status,
            "error_message": error,
            "details_json": dumps_json(dict(details)),
            "now": now,
        }
        for name in _RUN_COUNT_COLUMNS:
            binds[name] = int(counts.get(name, 0) or 0)
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"""
                UPDATE {T_CRM_RUNS}
                SET status = :status, error_message = :error_message, files_seen = :files_seen,
                    files_new = :files_new, files_changed = :files_changed, files_deleted = :files_deleted,
                    files_unchanged = :files_unchanged, files_failed = :files_failed,
                    details_json = :details_json, finished_at = :now, updated_at = :now
                WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id
                """,
                binds,
            )
            if cur.rowcount != 1:
                return False
            await self._record_on_source(
                conn,
                source_id,
                status=status,
                error=error,
                now=now,
                next_sync_at=next_sync_at,
                interval_days=schedule_interval_days,
                hour=schedule_hour,
            )
        return True

    async def recover_stale_runs(self, older_than: datetime) -> list[int]:
        """RUNNING runs without a heartbeat since ``older_than`` become FAILED (interrupted), with
        their in-flight file and their source's last-sync fields; one transaction per run."""
        rows = await self._db.fetch_all(
            f"SELECT id, source_id FROM {T_CRM_RUNS} WHERE status = 'RUNNING' AND updated_at < :threshold",
            {"threshold": older_than},
        )
        recovered: list[int] = []
        for row in rows:
            if await self._fail_interrupted_run(
                int(row["id"]), int(row["source_id"]), INTERRUPTED_RUN_REASON, stale_before=older_than
            ):
                recovered.append(int(row["id"]))
        if recovered:
            _log.warning("doc_intel: marked interrupted sync runs as FAILED: %s", recovered)
        return recovered

    async def release_interrupted_run(self, run_id: int, worker_id: str, *, reason: str = SHUTDOWN_RUN_REASON) -> bool:
        """Shutdown path: fail the run this worker was processing."""
        row = await self._db.fetch_one(
            f"SELECT id, source_id FROM {T_CRM_RUNS} WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id",
            {"id": int(run_id), "worker_id": worker_id[:128]},
        )
        if row is None:
            return False
        return await self._fail_interrupted_run(int(run_id), int(row["source_id"]), reason, worker_id=worker_id)

    # ---- files ------------------------------------------------------------------------------------------

    async def active_files(self, source_id: int) -> list[dict[str, Any]]:
        """The tracked (ACTIVE) files of a source, for the listing diff."""
        return await self._db.fetch_all(
            f"""
            SELECT id, drive_id, item_id, name, path, web_url, etag, ctag, quick_xor_hash, size_bytes,
                   modified_at, status, attempts
            FROM {T_CRM_FILES} WHERE source_id = :source_id AND state = 'ACTIVE'
            """,
            {"source_id": int(source_id)},
        )

    async def get_file(self, file_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(f"{_FILE_SELECT} WHERE f.id = :id", {"id": int(file_id)})

    async def list_files(
        self,
        source_id: int,
        *,
        state: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = ["f.source_id = :source_id"]
        binds: dict[str, Any] = {"source_id": int(source_id)}
        if state:
            filters.append("f.state = :state")
            binds["state"] = state
        if status:
            filters.append("f.status = :status")
            binds["status"] = status
        where = f"WHERE {' AND '.join(filters)}"
        rows = await self._db.fetch_all(
            f"""
            {_FILE_SELECT} {where}
            ORDER BY f.updated_at DESC, f.id DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
            """,
            {**binds, "offset": max(0, int(offset)), "limit": max(1, int(limit))},
        )
        total = await self._db.fetch_one(f"SELECT COUNT(*) AS total FROM {T_CRM_FILES} f {where}", binds)
        return rows, int((total or {}).get("total") or 0)

    async def upsert_file(self, source_id: int, remote: RemoteFile, *, run_id: int, reset_attempts: bool = True) -> int:
        """Track a listed file that must be processed: insert it when unknown, else refresh its
        metadata (re-activating a DELETED one) and re-arm it: every stage PENDING, status PENDING.
        ``reset_attempts`` (new content) restarts the retry budget; a plain retry keeps it."""
        meta = remote_columns(remote)
        async with self._db.connection() as conn:
            row = await self._db.fetch_one(
                f"""
                SELECT id FROM {T_CRM_FILES}
                WHERE source_id = :source_id AND drive_id = :drive_id AND item_id = :item_id FOR UPDATE
                """,
                {"source_id": int(source_id), "drive_id": remote.drive_id, "item_id": remote.item_id},
                conn=conn,
            )
            if row is None:
                now = utc_now()
                file_id = await self._db.execute(
                    f"""
                    INSERT INTO {T_CRM_FILES} (
                        source_id, drive_id, item_id, name, path, web_url, etag, ctag, quick_xor_hash,
                        size_bytes, modified_at, state, status, download_status, extraction_status,
                        intelligence_status, entities_status, persist_status, stage_details, attempts,
                        last_run_id, first_seen_at, last_seen_at, updated_at
                    ) VALUES (
                        :source_id, :drive_id, :item_id, :name, :path, :web_url, :etag, :ctag, :quick_xor_hash,
                        :size_bytes, :modified_at, 'ACTIVE', 'PENDING', 'PENDING', 'PENDING',
                        'PENDING', 'PENDING', 'PENDING', '{{}}', 0,
                        :run_id, :now, :now, :now
                    ) RETURNING id INTO :out_id
                    """,
                    {
                        **meta,
                        "source_id": int(source_id),
                        "drive_id": remote.drive_id,
                        "item_id": remote.item_id,
                        "run_id": int(run_id),
                        "now": now,
                    },
                    conn=conn,
                    return_id=True,
                )
                assert file_id is not None
                return int(file_id)
            file_id = int(row["id"])
            columns: dict[str, Any] = {
                **meta,
                "state": FILE_ACTIVE,
                "deleted_at": None,
                "status": FILE_PENDING,
                "failed_stage": None,
                "error_message": None,
                "last_seen_at": NOW,
            }
            if reset_attempts:
                columns["attempts"] = 0
            await self._update_file_in(
                conn, file_id, stages=[StageChange(stage, STAGE_PENDING) for stage in CRM_STAGES], columns=columns
            )
            return file_id

    async def refresh_file_metadata(self, file_id: int, remote: RemoteFile) -> None:
        """An unchanged file that was renamed or moved: new name/path/url, same content and state."""
        now = utc_now()
        await self._db.execute(
            f"""
            UPDATE {T_CRM_FILES}
            SET name = :name, path = :path, web_url = :web_url, etag = :etag, ctag = :ctag,
                quick_xor_hash = :quick_xor_hash, size_bytes = :size_bytes, modified_at = :modified_at,
                last_seen_at = :now
            WHERE id = :id AND state = 'ACTIVE'
            """,
            {**remote_columns(remote), "id": int(file_id), "now": now},
        )

    async def mark_seen(self, file_ids: Sequence[int]) -> None:
        """Stamp last_seen_at on listed files (their state, and updated_at, are untouched)."""
        now = utc_now()
        for chunk in _chunks(list(dict.fromkeys(int(i) for i in file_ids))):
            placeholders, binds = _in_list("f", chunk)
            await self._db.execute(
                f"UPDATE {T_CRM_FILES} SET last_seen_at = :now WHERE id IN ({placeholders})", {**binds, "now": now}
            )

    async def mark_files_deleted(self, file_ids: Sequence[int]) -> int:
        """Files gone from a COMPLETE listing: DELETED, and their entities WITHDRAWN (soft; history kept)."""
        now = utc_now()
        marked = 0
        for chunk in _chunks(list(dict.fromkeys(int(i) for i in file_ids))):
            placeholders, binds = _in_list("f", chunk)
            async with self._db.connection() as conn:
                cur = conn.cursor()
                await cur.execute(
                    f"""
                    UPDATE {T_CRM_FILES} SET state = 'DELETED', deleted_at = :now, updated_at = :now
                    WHERE id IN ({placeholders}) AND state = 'ACTIVE'
                    """,
                    {**binds, "now": now},
                )
                marked += max(0, int(cur.rowcount or 0))
                await self._db.execute(
                    f"""
                    UPDATE {T_CRM_ENTITIES} SET status = 'WITHDRAWN', withdrawn_at = :now, updated_at = :now
                    WHERE source_file_id IN ({placeholders}) AND status = 'ACTIVE'
                    """,
                    {**binds, "now": now},
                    conn=conn,
                )
        return marked

    async def begin_file(self, file_id: int, run_id: int) -> bool:
        """PENDING/FAILED ACTIVE file -> PROCESSING for ``run_id`` (one more attempt, stages reset)."""
        return await self._update_file(
            file_id,
            stages=[StageChange(stage, STAGE_PENDING) for stage in CRM_STAGES],
            columns={
                "status": FILE_PROCESSING,
                "attempts": INCREMENT,
                "last_run_id": int(run_id),
                "failed_stage": None,
                "error_message": None,
            },
            expect_status=(FILE_PENDING, FILE_FAILED),
            expect_state=FILE_ACTIVE,
        )

    async def set_file_stage(
        self,
        file_id: int,
        stage: str,
        status: str,
        *,
        error: str | None = None,
        metrics: Mapping[str, Any] | None = None,
    ) -> bool:
        """Update one stage of a PROCESSING file; False when it is no longer PROCESSING."""
        return await self._update_file(
            file_id,
            stages=[StageChange(stage, status, error=error, metrics=metrics)],
            columns={},
            expect_status=(FILE_PROCESSING,),
        )

    async def fail_file(
        self,
        file_id: int,
        stage: str,
        reason: str,
        *,
        completed: Sequence[str] = (),
        pending: Sequence[str] = (),
        metrics: Mapping[str, Any] | None = None,
    ) -> bool:
        """End processing FAILED at ``stage``: ``completed`` stages are stamped COMPLETED and
        ``pending`` ones (that never ran, e.g. one marked RUNNING in advance) set back to PENDING."""
        reason = truncate_utf8(reason or "Failed", MAX_ERROR_BYTES) or "Failed"
        stages = [StageChange(s, STAGE_PENDING) for s in pending if s != stage]
        stages += [StageChange(s, STAGE_COMPLETED) for s in completed if s != stage]
        stages.append(StageChange(stage, STAGE_FAILED, error=reason, metrics=metrics))
        return await self._update_file(
            file_id,
            stages=stages,
            columns={"status": FILE_FAILED, "failed_stage": stage, "error_message": reason, "processed_at": NOW},
            expect_status=(FILE_PROCESSING,),
        )

    async def complete_file(
        self,
        file_id: int,
        *,
        source_id: int,
        account_id: int | None,
        entities: Sequence[EntityRow],
        result: Mapping[str, Any] | None,
        warnings: Sequence[Mapping[str, Any]],
        is_valid: bool | None,
        content_sha256: str | None,
        metrics: Mapping[str, Any] | None = None,
    ) -> bool:
        """The persist stage, in ONE transaction: the file's entities are replaced (see
        ``replace_file_entities``) and the file becomes COMPLETED with its result JSON.
        False (nothing written) when the file is no longer PROCESSING."""
        async with self._db.connection() as conn:
            current = await self._db.fetch_one(
                f"SELECT status, state FROM {T_CRM_FILES} WHERE id = :id FOR UPDATE", {"id": int(file_id)}, conn=conn
            )
            if current is None or current.get("status") != FILE_PROCESSING or current.get("state") != FILE_ACTIVE:
                return False
            summary = await self.replace_file_entities(
                file_id, entities, source_id=source_id, account_id=account_id, conn=conn
            )
            return await self._update_file_in(
                conn,
                file_id,
                stages=[StageChange("persist", STAGE_COMPLETED, metrics={**summary, **dict(metrics or {})})],
                columns={
                    "status": FILE_COMPLETED,
                    "failed_stage": None,
                    "error_message": None,
                    "result_json": dumps_json(dict(result or {})),
                    "warnings_json": dumps_json(list(warnings or [])),
                    "entity_count": len(entities),
                    "is_valid": None if is_valid is None else int(bool(is_valid)),
                    "content_sha256": content_sha256,
                    "processed_at": NOW,
                },
                expect_status=(FILE_PROCESSING,),
            )

    async def reset_file_for_retry(self, file_id: int) -> bool:
        """FAILED ACTIVE file -> PENDING with a fresh retry budget; the next run processes it."""
        return await self._update_file(
            file_id,
            stages=[StageChange(stage, STAGE_PENDING) for stage in CRM_STAGES],
            columns={"status": FILE_PENDING, "failed_stage": None, "error_message": None, "attempts": 0},
            expect_status=(FILE_FAILED,),
            expect_state=FILE_ACTIVE,
        )

    async def counts_by_source(self, source_id: int | None = None) -> dict[int, dict[str, int]]:
        """{source_id: {files_active, files_failed, files_deleted, entities_active}}."""
        where, binds = ("WHERE source_id = :source_id", {"source_id": int(source_id)}) if source_id is not None else ("", {})
        file_rows = await self._db.fetch_all(
            f"SELECT source_id, state, status, COUNT(*) AS n FROM {T_CRM_FILES} {where} GROUP BY source_id, state, status",
            binds,
        )
        entity_where = f"{where} AND status = 'ACTIVE'" if where else "WHERE status = 'ACTIVE'"
        entity_rows = await self._db.fetch_all(
            f"SELECT source_id, COUNT(*) AS n FROM {T_CRM_ENTITIES} {entity_where} GROUP BY source_id", binds
        )
        out: dict[int, dict[str, int]] = {}
        for r in file_rows:
            counts = out.setdefault(int(r["source_id"]), dict(_EMPTY_COUNTS))
            n = int(r.get("n") or 0)
            if r.get("state") == FILE_DELETED:
                counts["files_deleted"] += n
            else:
                counts["files_active"] += n
                if r.get("status") == FILE_FAILED:
                    counts["files_failed"] += n
        for r in entity_rows:
            out.setdefault(int(r["source_id"]), dict(_EMPTY_COUNTS))["entities_active"] = int(r.get("n") or 0)
        return out

    # ---- entities ---------------------------------------------------------------------------------------

    async def replace_file_entities(
        self,
        file_id: int,
        entities: Sequence[EntityRow],
        *,
        source_id: int,
        account_id: int | None,
        conn: Any = None,
    ) -> dict[str, int]:
        """In ONE transaction: the file's ACTIVE entities become WITHDRAWN (withdrawn_at stamped,
        history kept) and ``entities`` are inserted ACTIVE. Returns {"withdrawn", "inserted"}."""
        if conn is None:
            async with self._db.connection() as own:
                return await self.replace_file_entities(
                    file_id, entities, source_id=source_id, account_id=account_id, conn=own
                )
        now = utc_now()
        cur = conn.cursor()
        await cur.execute(
            f"""
            UPDATE {T_CRM_ENTITIES} SET status = 'WITHDRAWN', withdrawn_at = :now, updated_at = :now
            WHERE source_file_id = :file_id AND status = 'ACTIVE'
            """,
            {"file_id": int(file_id), "now": now},
        )
        withdrawn = max(0, int(cur.rowcount or 0))
        for entity in entities[:MAX_ENTITIES_PER_FILE]:
            await self._db.execute(
                _ENTITY_INSERT,
                {
                    "source_id": int(source_id),
                    "source_file_id": int(file_id),
                    "account_id": account_id,
                    "entity_type": entity.entity_type,
                    "display_value": truncate_utf8(entity.display_value, MAX_DISPLAY_BYTES),
                    "match_key": truncate_utf8(entity.match_key, MAX_MATCH_KEY_BYTES),
                    "confidence": entity.confidence,
                    "is_valid": None if entity.is_valid is None else int(bool(entity.is_valid)),
                    "fields_json": dumps_json(entity.fields),
                    "issues_json": dumps_json(list(entity.issues)),
                    "now": now,
                },
                conn=conn,
            )
        return {"withdrawn": withdrawn, "inserted": min(len(entities), MAX_ENTITIES_PER_FILE)}

    async def withdraw_file_entities(self, file_id: int) -> int:
        """ACTIVE entities of a file -> WITHDRAWN (e.g. the file was deleted)."""
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"""
                UPDATE {T_CRM_ENTITIES} SET status = 'WITHDRAWN', withdrawn_at = :now, updated_at = :now
                WHERE source_file_id = :file_id AND status = 'ACTIVE'
                """,
                {"file_id": int(file_id), "now": utc_now()},
            )
            return max(0, int(cur.rowcount or 0))

    async def list_entities(
        self,
        *,
        source_id: int | None = None,
        entity_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        filters: list[str] = []
        binds: dict[str, Any] = {}
        if source_id is not None:
            filters.append("e.source_id = :source_id")
            binds["source_id"] = int(source_id)
        if entity_type:
            filters.append("e.entity_type = :entity_type")
            binds["entity_type"] = entity_type
        if status:
            filters.append("e.status = :status")
            binds["status"] = status
        text = " ".join(str(q or "").split()).lower()
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append("(LOWER(e.display_value) LIKE :q ESCAPE '\\' OR LOWER(e.match_key) LIKE :q ESCAPE '\\')")
            binds["q"] = f"%{escaped}%"
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = await self._db.fetch_all(
            f"""
            {_ENTITY_SELECT} {where}
            ORDER BY e.created_at DESC, e.id DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
            """,
            {**binds, "offset": max(0, int(offset)), "limit": max(1, int(limit))},
        )
        total = await self._db.fetch_one(f"SELECT COUNT(*) AS total FROM {T_CRM_ENTITIES} e {where}", binds)
        return rows, int((total or {}).get("total") or 0)

    async def get_entity(self, entity_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(f"{_ENTITY_SELECT} WHERE e.id = :id", {"id": int(entity_id)})

    # ---- monitoring --------------------------------------------------------------------------------------

    async def store_stats(self) -> dict[str, int]:
        """A cheap probe of the CRM store: live sources, ACTIVE entities and tracked files."""
        sources = await self._db.fetch_one(
            f"SELECT COUNT(*) AS n FROM {T_CRM_SOURCES} WHERE status <> 'DELETED'"
        )
        entities = await self._db.fetch_one(f"SELECT COUNT(*) AS n FROM {T_CRM_ENTITIES} WHERE status = 'ACTIVE'")
        files = await self._db.fetch_one(f"SELECT COUNT(*) AS n FROM {T_CRM_FILES} WHERE state = 'ACTIVE'")
        return {
            "sources": int((sources or {}).get("n") or 0),
            "entities_active": int((entities or {}).get("n") or 0),
            "files_active": int((files or {}).get("n") or 0),
        }

    async def last_runs(self) -> dict[int, dict[str, Any]]:
        """The latest FINISHED run of each source."""
        rows = await self._db.fetch_all(
            f"""
            SELECT id, source_id, trigger_type, status, error_message, files_failed, created_at,
                   started_at, finished_at
            FROM (
                SELECT r.id, r.source_id, r.trigger_type, r.status, r.error_message, r.files_failed,
                       r.created_at, r.started_at, r.finished_at,
                       ROW_NUMBER() OVER (PARTITION BY r.source_id ORDER BY r.created_at DESC, r.id DESC) AS rn
                FROM {T_CRM_RUNS} r
                WHERE r.status IN ('COMPLETED', 'PARTIAL', 'FAILED')
            )
            WHERE rn = 1
            """
        )
        return {int(r["source_id"]): r for r in rows}

    async def persist_failures(self, run_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
        """{run_id: {"n", "reason"}} for files whose persist stage failed in those runs."""
        out: dict[int, dict[str, Any]] = {}
        for chunk in _chunks(list(dict.fromkeys(int(i) for i in run_ids))):
            placeholders, binds = _in_list("r", chunk)
            rows = await self._db.fetch_all(
                f"""
                SELECT last_run_id AS run_id, COUNT(*) AS n, MAX(error_message) AS reason
                FROM {T_CRM_FILES}
                WHERE status = 'FAILED' AND failed_stage = 'persist' AND state = 'ACTIVE'
                  AND last_run_id IN ({placeholders})
                GROUP BY last_run_id
                """,
                binds,
            )
            for r in rows:
                out[int(r["run_id"])] = {"n": int(r.get("n") or 0), "reason": r.get("reason")}
        return out

    async def stuck_runs(self, heartbeat_before: datetime) -> list[dict[str, Any]]:
        """RUNNING runs whose heartbeat (updated_at) stopped before ``heartbeat_before``."""
        return await self._db.fetch_all(
            f"""
            SELECT r.id, r.source_id, r.started_at, r.updated_at, s.name AS source_name
            FROM {T_CRM_RUNS} r
            LEFT JOIN {T_CRM_SOURCES} s ON s.id = r.source_id
            WHERE r.status = 'RUNNING' AND r.updated_at < :before
            ORDER BY r.started_at
            FETCH FIRST 50 ROWS ONLY
            """,
            {"before": heartbeat_before},
        )

    async def failed_files_since(self, since: datetime) -> list[dict[str, Any]]:
        """FAILED files of live sources (a deleted source's history is not a failure to act on)."""
        return await self._db.fetch_all(
            f"""
            SELECT f.id, f.source_id, f.name, f.path, f.failed_stage, f.error_message, f.processed_at,
                   f.updated_at, s.name AS source_name, a.name AS account_name
            FROM {T_CRM_FILES} f
            LEFT JOIN {T_CRM_SOURCES} s ON s.id = f.source_id
            LEFT JOIN AIVA_accounts a ON a.id = s.account_id
            WHERE f.status = 'FAILED' AND f.state = 'ACTIVE' AND s.status <> 'DELETED'
              AND COALESCE(f.processed_at, f.updated_at) >= :since
            ORDER BY COALESCE(f.processed_at, f.updated_at) DESC, f.id DESC
            FETCH FIRST 500 ROWS ONLY
            """,
            {"since": since},
        )

    async def failed_runs_since(self, since: datetime) -> list[dict[str, Any]]:
        """FAILED runs of live sources."""
        return await self._db.fetch_all(
            f"""
            SELECT r.id, r.source_id, r.trigger_type, r.error_message, r.finished_at, r.updated_at,
                   s.name AS source_name, a.name AS account_name
            FROM {T_CRM_RUNS} r
            LEFT JOIN {T_CRM_SOURCES} s ON s.id = r.source_id
            LEFT JOIN AIVA_accounts a ON a.id = s.account_id
            WHERE r.status = 'FAILED' AND s.status <> 'DELETED' AND COALESCE(r.finished_at, r.updated_at) >= :since
            ORDER BY COALESCE(r.finished_at, r.updated_at) DESC, r.id DESC
            FETCH FIRST 500 ROWS ONLY
            """,
            {"since": since},
        )

    async def run_activity(self, limit: int) -> list[dict[str, Any]]:
        """The most recently changed runs (their latest state), newest first."""
        return await self._db.fetch_all(
            f"""
            SELECT r.id, r.source_id, r.trigger_type, r.status, r.files_seen, r.files_new, r.files_changed,
                   r.files_deleted, r.files_unchanged, r.files_failed, r.error_message, r.created_at,
                   r.started_at, r.finished_at, r.updated_at, s.name AS source_name
            FROM {T_CRM_RUNS} r
            LEFT JOIN {T_CRM_SOURCES} s ON s.id = r.source_id
            ORDER BY r.updated_at DESC, r.id DESC
            FETCH FIRST :limit ROWS ONLY
            """,
            {"limit": max(1, int(limit))},
        )

    async def file_activity(self, limit: int) -> list[dict[str, Any]]:
        """The most recently changed files (their latest state), newest first."""
        return await self._db.fetch_all(
            f"""
            SELECT f.id, f.source_id, f.name, f.state, f.status, f.failed_stage, f.error_message,
                   f.entity_count, f.download_status, f.extraction_status, f.intelligence_status,
                   f.entities_status, f.persist_status, f.updated_at, s.name AS source_name
            FROM {T_CRM_FILES} f
            LEFT JOIN {T_CRM_SOURCES} s ON s.id = f.source_id
            ORDER BY f.updated_at DESC, f.id DESC
            FETCH FIRST :limit ROWS ONLY
            """,
            {"limit": max(1, int(limit))},
        )

    # ---- internals ---------------------------------------------------------------------------------------

    async def _update_source(
        self, source_id: int, columns: Mapping[str, Any], *, updated_by: int | None, admin: bool
    ) -> bool:
        sets: list[str] = []
        binds: dict[str, Any] = {"id": int(source_id)}
        for name, value in columns.items():
            if name not in _SOURCE_WRITABLE:
                raise ValueError(f"Column not writable: {name!r}")
            sets.append(f"{name} = :col_{name}")
            binds[f"col_{name}"] = value
        if admin:
            sets += ["updated_by = :updated_by", "updated_at = :now"]
            binds.update(updated_by=updated_by, now=utc_now())
        if not sets:
            return True
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(
                f"UPDATE {T_CRM_SOURCES} SET {', '.join(sets)} WHERE id = :id AND status <> 'DELETED'", binds
            )
            return cur.rowcount == 1

    async def _record_on_source(
        self,
        conn: Any,
        source_id: int,
        *,
        status: str,
        error: str | None,
        now: datetime,
        next_sync_at: datetime | None,
        interval_days: int | None,
        hour: int | None,
    ) -> None:
        sets = [
            "last_sync_at = :now",
            "last_sync_status = :status",
            "last_sync_error = :error",
            "last_success_at = CASE WHEN :status = 'COMPLETED' THEN :now ELSE last_success_at END",
        ]
        binds: dict[str, Any] = {"id": int(source_id), "now": now, "status": status, "error": error}
        if next_sync_at is not None and interval_days is not None and hour is not None:
            sets.append(
                "next_sync_at = CASE WHEN sync_enabled = 1 AND status = 'ACTIVE' AND sync_interval_days = :interval"
                " AND sync_hour = :hour THEN :next_sync_at ELSE next_sync_at END"
            )
            binds.update(next_sync_at=next_sync_at, interval=int(interval_days), hour=int(hour))
        await self._db.execute(
            f"UPDATE {T_CRM_SOURCES} SET {', '.join(sets)} WHERE id = :id AND status <> 'DELETED'", binds, conn=conn
        )

    async def _fail_interrupted_run(
        self,
        run_id: int,
        source_id: int,
        reason: str,
        *,
        stale_before: datetime | None = None,
        worker_id: str | None = None,
    ) -> bool:
        now = utc_now()
        sql = f"""
            UPDATE {T_CRM_RUNS}
            SET status = 'FAILED', error_message = :reason, finished_at = :now, updated_at = :now
            WHERE id = :id AND status = 'RUNNING'
        """
        binds: dict[str, Any] = {"id": int(run_id), "reason": reason, "now": now}
        if stale_before is not None:
            sql += " AND updated_at < :threshold"
            binds["threshold"] = stale_before
        if worker_id is not None:
            sql += " AND worker_id = :worker_id"
            binds["worker_id"] = worker_id[:128]
        async with self._db.connection() as conn:
            cur = conn.cursor()
            await cur.execute(sql, binds)
            if cur.rowcount != 1:
                return False
            in_flight = await self._db.fetch_all(
                f"""
                SELECT id, download_status, extraction_status, intelligence_status, entities_status, persist_status
                FROM {T_CRM_FILES} WHERE last_run_id = :run_id AND status = 'PROCESSING'
                """,
                {"run_id": int(run_id)},
                conn=conn,
            )
            for row in in_flight:
                stage = running_file_stage(row)
                await self._update_file_in(
                    conn,
                    int(row["id"]),
                    stages=[StageChange(stage, STAGE_FAILED, error=INTERRUPTED_FILE_REASON)],
                    columns={
                        "status": FILE_FAILED,
                        "failed_stage": stage,
                        "error_message": INTERRUPTED_FILE_REASON,
                        "processed_at": NOW,
                    },
                    expect_status=(FILE_PROCESSING,),
                )
            await self._record_on_source(
                conn, source_id, status=RUN_FAILED, error=reason, now=now, next_sync_at=None, interval_days=None, hour=None
            )
        return True

    async def _update_file(
        self,
        file_id: int,
        *,
        stages: Sequence[StageChange],
        columns: Mapping[str, Any],
        expect_status: Sequence[str] | None = None,
        expect_state: str | None = None,
    ) -> bool:
        async with self._db.connection() as conn:
            return await self._update_file_in(
                conn, file_id, stages=stages, columns=columns, expect_status=expect_status, expect_state=expect_state
            )

    async def _update_file_in(
        self,
        conn: Any,
        file_id: int,
        *,
        stages: Sequence[StageChange],
        columns: Mapping[str, Any],
        expect_status: Sequence[str] | None = None,
        expect_state: str | None = None,
    ) -> bool:
        """Stage changes + column values on one file inside ``conn``'s transaction (row locked).

        Returns False (and changes nothing) when the row is gone or no longer matches
        ``expect_status`` / ``expect_state``.
        """
        now = utc_now()
        file_id = int(file_id)
        current = await self._db.fetch_one(
            f"SELECT status, state, stage_details FROM {T_CRM_FILES} WHERE id = :id FOR UPDATE",
            {"id": file_id},
            conn=conn,
        )
        if current is None:
            return False
        if expect_status and current.get("status") not in expect_status:
            return False
        if expect_state and current.get("state") != expect_state:
            return False
        sets: list[str] = []
        binds: dict[str, Any] = {"id": file_id, "now": now}
        if stages:
            details = loads_json(current.get("stage_details"), {})
            for i, change in enumerate(stages):
                details = apply_file_stage_change(details, change, now=now)
                sets.append(f"{file_stage_column(change.stage)} = :stage_{i}")
                binds[f"stage_{i}"] = change.status
            sets.append("stage_details = :stage_details")
            binds["stage_details"] = dumps_json(details)
        for name, value in columns.items():
            if name not in _FILE_WRITABLE:
                raise ValueError(f"Column not writable: {name!r}")
            if value is NOW:
                sets.append(f"{name} = :now")
            elif value is INCREMENT:
                sets.append(f"{name} = {name} + 1")
            else:
                sets.append(f"{name} = :col_{name}")
                binds[f"col_{name}"] = value
        sets.append("updated_at = :now")
        cur = conn.cursor()
        await cur.execute(f"UPDATE {T_CRM_FILES} SET {', '.join(sets)} WHERE id = :id", binds)
        return cur.rowcount == 1


def stale_threshold(heartbeat_seconds: float) -> timedelta:
    """How long a RUNNING run may go without a heartbeat before it is recovered as interrupted."""
    return timedelta(seconds=max(300.0, 2 * float(heartbeat_seconds)))
