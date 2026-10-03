"""Flow 2: SharePoint / OneDrive -> the internal CRM store ("Sync now" and scheduled syncs).

A sync run (a row in AIVA_crm_sync_runs) goes

    credentials (decrypt) -> sign-in (token) -> site / library / folder (ids cached on the source)
    -> folder listing -> diff against the tracked files (new / changed / unchanged / deleted)
    -> per new or changed file: download -> extraction -> intelligence -> entities -> persist

and ends COMPLETED, PARTIAL (some files failed, or the listing was incomplete) or FAILED
(credentials, sign-in, resolution or listing failed; the reason is the run's error). Files
missing from the listing are marked deleted, and their entities withdrawn, ONLY when the
listing is complete. Files are processed one at a time and one failing never stops the run.

QUEUED runs are the work queue: "Sync now" and the scheduler insert them (the unique index
allows one QUEUED/RUNNING run per source) and ``SyncWorker`` claims the oldest with a
conditional UPDATE, one run at a time. Extraction runs in a child process that shares the
process-wide extraction slot with knowledge-document imports. Blocking Graph and extraction
calls run on the module's own thread pool (``threads.run_blocking``), never on the default
executor that chat's knowledge search uses; nothing heavy runs on the event loop.

Secrets: the client secret is decrypted only for the Graph session of a run, a connection
test or a health check. It is never returned, logged, audited or put into exception text;
text that could echo it is scrubbed first.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import socket
import tempfile
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from pydantic import SecretStr, ValidationError

from backend.auth.deps import UserContext
from backend.config import get_settings as get_backend_settings
from backend.doc_intel import crm_extraction as crm_extraction_module
from backend.doc_intel import crypto as crypto_module
from backend.doc_intel import graph_source as graph_module
from backend.doc_intel import schedule
from backend.doc_intel.constants import (
    ALLOWED_EXTENSIONS,
    CRM_STAGES,
    MAX_ERROR_BYTES,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_PARTIAL,
    STAGE_COMPLETED,
    STAGE_RUNNING,
)
from backend.doc_intel.crm_extraction import CrmExtractionFailed, CrmExtractionResult
from backend.doc_intel.crm_repo import (
    FILE_ACTIVE,
    FILE_FAILED,
    FILE_PENDING,
    FILE_PROCESSING,
    MAX_CLIENT_ENC_BYTES,
    MAX_DRIVE_NAME_BYTES,
    MAX_FILE_NAME_BYTES,
    MAX_FILE_PATH_BYTES,
    MAX_FOLDER_PATH_BYTES,
    MAX_HASH_BYTES,
    MAX_HINT_BYTES,
    MAX_ITEM_ID_BYTES,
    MAX_SECRET_ENC_BYTES,
    MAX_SITE_URL_BYTES,
    MAX_SOURCE_NAME_BYTES,
    MAX_TAG_BYTES,
    MAX_TENANT_ENC_BYTES,
    MAX_URL_BYTES,
    SHUTDOWN_RUN_REASON,
    SOURCE_ACTIVE,
    SOURCE_DELETED,
    SOURCE_DELETED_REASON,
    TRIGGER_MANUAL,
    RunAlreadyActive,
    build_entity_rows,
    entity_to_out,
    extensions_from_column,
    file_to_out,
    fits,
    run_to_out,
    schema_hints,
    source_to_out,
    stale_threshold,
)
from backend.doc_intel.crypto import SecretsUnavailable
from backend.doc_intel.embedding import scrub_secrets
from backend.doc_intel.graph_source import GraphCredentials, GraphFailed, ListingResult, RemoteFile, ResolvedTarget
from backend.doc_intel.kb_repo import parse_utc
from backend.doc_intel.runtime import ServiceUnavailableError
from backend.doc_intel.schemas import (
    ConnectionStepOut,
    ConnectionTestOut,
    CrmEntityListOut,
    CrmEntityOut,
    SourceCreate,
    SourceFileListOut,
    SourceFileOut,
    SourceOut,
    SourceUpdate,
    SyncRunListOut,
    SyncRunOut,
)
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.textutil import iso_utc, truncate_utf8, utc_now
from backend.doc_intel.threads import run_blocking
from backend.exceptions import BadRequestError, ConflictError, NotFoundError
from backend.services.audit import write_audit_log
from backend.services.error_log import persist_error_log

_log = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS: Final[tuple[str, ...]] = tuple(ALLOWED_EXTENSIONS)
# A failed file whose content did not change is retried by later runs while attempts < this.
MAX_FILE_ATTEMPTS: Final = 3
HEARTBEAT_SECONDS: Final = 60.0
# "Test connection" answers well under nginx's 60 s proxy timeout.
CONNECTION_TEST_TIMEOUT_SECONDS: Final = 50.0
TEMP_PREFIX: Final = "aiva-docintel-crm-"
TEMP_MAX_AGE: Final = timedelta(hours=24)
_CHILD_STAGES: Final[tuple[str, ...]] = ("extraction", "intelligence", "entities")
# CrmExtractionFailed codes raised before the document was extracted: in the parent (LLM
# intelligence refused, a library missing, the file gone or of an unsupported type) or first
# thing in the child (the CRM library import). Every other code comes from the child's
# progress, so the stages before the failed one did complete.
_NOT_STARTED_CODES: Final = frozenset({"not_supported", "unavailable", "source_missing", "unsupported", "missing_dependency"})
_RUN_COUNTS: Final[tuple[str, ...]] = (
    "files_seen",
    "files_new",
    "files_changed",
    "files_deleted",
    "files_unchanged",
    "files_failed",
)

SOURCE_NOT_FOUND: Final = "Source not found"
SYNC_ALREADY_ACTIVE: Final = "A sync is already queued or running for this source"
SOURCE_DISABLED_SYNC: Final = "This source is disabled — enable it first"
SYNC_BLOCKS_DELETE: Final = "A sync is queued or running for this source — delete it once the sync has finished"
_STATE_CHANGED: Final = "The item changed state in the meantime; refresh and try again"
# The formats graph_source accepts at sign-in, checked when the credentials are saved.
_GUID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_DNS_NAME = re.compile(r"(?=.{3,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"*:<>?|]')

KEY_ACTION: Final = (
    "Set DOC_INTEL_SECRETS_KEY on the server (generate one with "
    "`python -m backend.doc_intel.crypto generate-key`) and restart the backend"
)
DECRYPT_ACTION: Final = (
    "The stored credentials cannot be decrypted with the current DOC_INTEL_SECRETS_KEY: add the "
    "previous key back to the key list, or enter the Tenant ID, Client ID and Client Secret again"
)

AuditFn = Callable[..., Awaitable[None]]
ErrorLogFn = Callable[..., Awaitable[None]]


class _RunAbandoned(Exception):
    """The run is no longer this worker's RUNNING run (recovered as interrupted); stop working on it."""


class _StopRequested(Exception):
    """The worker is shutting down; leave the run for ``SyncWorker.stop`` to release."""


def secrets_action(ex: SecretsUnavailable) -> str:
    return DECRYPT_ACTION if getattr(ex, "code", "") == "decrypt_failed" else KEY_ACTION


def decrypt_credentials(row: Mapping[str, Any] | None, box: Any) -> GraphCredentials:
    """The plaintext credentials of a source from ``CrmRepo.get_credentials``. Raises
    SecretsUnavailable (incomplete or undecryptable); the message never includes a value."""
    if not row or not all(row.get(k) for k in ("tenant_id_enc", "client_id_enc", "client_secret_enc")):
        raise SecretsUnavailable(
            "The stored credentials are incomplete — enter the Tenant ID, Client ID and Client Secret again",
            code="decrypt_failed",
        )
    return GraphCredentials(
        tenant_id=box.decrypt(row["tenant_id_enc"]),
        client_id=box.decrypt(row["client_id_enc"]),
        client_secret=box.decrypt(row["client_secret_enc"]),
    )


# ---- input normalization (pure) ------------------------------------------------------------------


def normalize_extensions(values: Sequence[str]) -> list[str]:
    """Lower-cased, dotted, de-duplicated; only the types extraction supports (.pdf / .docx)."""
    out: list[str] = []
    for raw in values:
        ext = str(raw or "").strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = f".{ext}"
        if ext not in ALLOWED_EXTENSIONS:
            shown = ext if len(ext) <= 20 else ext[:19] + "…"
            raise BadRequestError(
                f"Unsupported file type: {shown} (only {', '.join(SUPPORTED_EXTENSIONS)} can be processed)"
            )
        if ext not in out:
            out.append(ext)
    if not out:
        raise BadRequestError("Select at least one file type")
    return out


def clean_name(value: str) -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise BadRequestError("Name is required")
    if not fits(name, MAX_SOURCE_NAME_BYTES):
        raise BadRequestError("Name is too long")
    return name


def clean_tenant_id(value: str) -> str:
    """The Directory (tenant) ID: a GUID, or a domain such as contoso.onmicrosoft.com."""
    text = str(value or "").strip()
    if not text:
        raise BadRequestError("Tenant ID is required")
    if not (_GUID.fullmatch(text) or _DNS_NAME.fullmatch(text.lower())):
        raise BadRequestError("The Tenant ID must be a GUID (the Directory ID) or a domain such as contoso.onmicrosoft.com")
    return text


def clean_client_id(value: str) -> str:
    """The app registration's Application (client) ID: a GUID."""
    text = str(value or "").strip()
    if not text:
        raise BadRequestError("Client ID is required")
    if not _GUID.fullmatch(text):
        raise BadRequestError("The Client ID must be the app's Application (client) ID, a GUID")
    return text


def clean_secret(value: SecretStr) -> str:
    """The plaintext client secret (whitespace around it removed). Messages never include it."""
    secret = value.get_secret_value().strip()
    if not secret:
        raise BadRequestError("Client secret is required")
    if _CONTROL.search(secret):
        raise BadRequestError("Client secret contains invalid characters")
    return secret


def clean_drive_name(value: str | None) -> str | None:
    """Document library name; None = the site's default library."""
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if not fits(text, MAX_DRIVE_NAME_BYTES):
        raise BadRequestError("Library name is too long")
    return text


def clean_folder_path(value: str | None) -> str | None:
    """``/A/B`` inside the library; None = the library root."""
    parts = [p.strip() for p in str(value or "").replace("\\", "/").split("/") if p.strip()]
    if any(p in (".", "..") for p in parts):
        raise BadRequestError("The folder path must not contain '.' or '..'")
    if any(_BAD_NAME_CHARS.search(p) for p in parts):
        raise BadRequestError('A folder name in the path contains a character SharePoint does not allow (" * : < > ? |)')
    if not parts:
        return None
    path = "/" + "/".join(parts)
    if not fits(path, MAX_FOLDER_PATH_BYTES):
        raise BadRequestError("Folder path is too long")
    return path


def mask(value: str | None) -> str | None:
    """Audit form of an identifier: its last four characters only."""
    if not value:
        return None
    return "…" + value[-4:] if len(value) >= 8 else "…"


# ---- listing diff (pure) --------------------------------------------------------------------------


@dataclass
class SyncPlan:
    new: list[RemoteFile] = field(default_factory=list)
    changed: list[tuple[int, RemoteFile]] = field(default_factory=list)
    # unchanged content, but not processed yet (PENDING) or failed with attempts left
    retry: list[tuple[int, RemoteFile]] = field(default_factory=list)
    unchanged: list[tuple[int, RemoteFile]] = field(default_factory=list)
    # unchanged files whose name / path / link changed (renamed or moved)
    renamed: list[tuple[int, RemoteFile]] = field(default_factory=list)
    # tracked files missing from the listing; only filled for a complete listing
    deleted: list[int] = field(default_factory=list)
    seen: int = 0
    ignored: int = 0


def _tag(value: str | None, max_bytes: int) -> str | None:
    return truncate_utf8(value, max_bytes) if value else None


def _second(value: Any) -> datetime | None:
    parsed = parse_utc(value)
    return parsed.replace(microsecond=0) if parsed is not None else None


def content_changed(row: Mapping[str, Any], remote: RemoteFile) -> bool:
    """cTag (content) first, then eTag, then quickXorHash, then size + modified time.
    Nothing comparable counts as changed."""
    for stored, current, limit in (
        (row.get("ctag"), remote.ctag, MAX_TAG_BYTES),
        (row.get("etag"), remote.etag, MAX_TAG_BYTES),
        (row.get("quick_xor_hash"), remote.quick_xor_hash, MAX_HASH_BYTES),
    ):
        if stored and current:
            return str(stored) != _tag(current, limit)
    size = row.get("size_bytes")
    stamp = _second(row.get("modified_at"))
    if size is not None and remote.size is not None and stamp is not None and remote.modified_at is not None:
        return int(size) != int(remote.size) or stamp != _second(remote.modified_at)
    return True


def needs_retry(row: Mapping[str, Any], max_attempts: int = MAX_FILE_ATTEMPTS) -> bool:
    status = row.get("status")
    if status == FILE_PENDING:
        return True
    if status in (FILE_FAILED, FILE_PROCESSING):
        return int(row.get("attempts") or 0) < max_attempts
    return False


def metadata_differs(row: Mapping[str, Any], remote: RemoteFile) -> bool:
    web_url = remote.web_url if remote.web_url and fits(remote.web_url, MAX_URL_BYTES) else None
    return (
        (row.get("name") or None) != (truncate_utf8(remote.name, MAX_FILE_NAME_BYTES) or None)
        or (row.get("path") or None) != (truncate_utf8(remote.path, MAX_FILE_PATH_BYTES) or None)
        or (row.get("web_url") or None) != web_url
        or (row.get("etag") or None) != _tag(remote.etag, MAX_TAG_BYTES)
        or row.get("size_bytes") != remote.size
        or _second(row.get("modified_at")) != _second(remote.modified_at)
    )


def plan_sync(
    active: Sequence[Mapping[str, Any]],
    files: Sequence[RemoteFile],
    *,
    complete: bool,
    max_attempts: int = MAX_FILE_ATTEMPTS,
) -> SyncPlan:
    """Diff a listing against the tracked (ACTIVE) files of a source.

    new = unknown (drive, item); changed = content differs (see ``content_changed``);
    retry = unchanged but PENDING, or FAILED with attempts left; deleted = tracked but not
    listed, and only when ``complete`` (a partial listing never deletes anything).
    """
    tracked = {(str(r.get("drive_id")), str(r.get("item_id"))): r for r in active}
    plan = SyncPlan()
    seen: set[tuple[str, str]] = set()
    for remote in files:
        key = (str(remote.drive_id or ""), str(remote.item_id or ""))
        if key in seen:
            continue
        if not all(key) or not fits(key[0], MAX_ITEM_ID_BYTES) or not fits(key[1], MAX_ITEM_ID_BYTES):
            plan.ignored += 1
            continue
        seen.add(key)
        plan.seen += 1
        row = tracked.get(key)
        if row is None:
            plan.new.append(remote)
        elif content_changed(row, remote):
            plan.changed.append((int(row["id"]), remote))
        elif needs_retry(row, max_attempts):
            plan.retry.append((int(row["id"]), remote))
        else:
            plan.unchanged.append((int(row["id"]), remote))
            if metadata_differs(row, remote):
                plan.renamed.append((int(row["id"]), remote))
    if complete:
        plan.deleted = [int(r["id"]) for key, r in tracked.items() if key not in seen]
    return plan


# ---- one run's working state ----------------------------------------------------------------------


@dataclass
class _RunContext:
    run_id: int
    source_id: int
    worker_id: str
    started_at: datetime
    source: dict[str, Any] = field(default_factory=dict)
    secret: str | None = field(default=None, repr=False)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_RUN_COUNTS, 0))
    details: dict[str, Any] = field(default_factory=dict)
    stage: str = CRM_STAGES[0]

    def scrub(self, text: Any) -> str:
        return scrub_secrets(str(text or ""), self.secret)


def _first_line(ex: BaseException) -> str:
    text = str(ex).strip()
    return (text.splitlines()[0] if text else type(ex).__name__)[:300]


def _sees_secret_hint(user: UserContext | None) -> bool:
    """The client secret's hint is for Super Admins (who manage it); Developers see only that
    a secret is set and when it changed."""
    return user is None or bool(user.is_super_admin)


def _close_quietly(graph: Any) -> None:
    try:
        graph.close()
    except Exception:
        _log.debug("doc_intel: Graph session did not close cleanly", exc_info=True)


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))


def _mb(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}".rstrip("0").rstrip(".")


def _int(value: Any) -> int | None:
    try:
        return None if value is None or value == "" else int(value)
    except (TypeError, ValueError):
        return None


class SyncService:
    """Source administration, "Sync now", and the processing of one sync run."""

    def __init__(
        self,
        *,
        db: Any,
        repo: Any,
        settings: DocIntelSettings,
        box_factory: Callable[[], Any] | None = None,
        graph_factory: Callable[[GraphCredentials], Any] | None = None,
        extract_fn: Callable[..., CrmExtractionResult] | None = None,
        site_url_validator: Callable[[str, DocIntelSettings], str] | None = None,
        hint_fn: Callable[[str], str] | None = None,
        audit: AuditFn | None = None,
        error_log: ErrorLogFn | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self._db = db
        self.repo = repo
        self.settings = settings
        self._box_factory = box_factory or (lambda: crypto_module.SecretBox.from_settings(settings))
        self._graph_factory = graph_factory or (lambda creds: graph_module.GraphSource(creds, settings))
        self._extract = extract_fn or crm_extraction_module.run_crm_extraction
        self._validate_site_url = site_url_validator or graph_module.validate_site_url
        self._hint = hint_fn or crypto_module.secret_hint
        self._audit = audit or self._write_audit
        self._error_log = error_log or self._write_error_log
        self.temp_root = temp_root
        self._wake: Callable[[], None] | None = None

    def set_wake_callback(self, wake: Callable[[], None] | None) -> None:
        self._wake = wake

    def wake_worker(self) -> None:
        if self._wake is not None:
            try:
                self._wake()
            except Exception:
                _log.warning("doc_intel: could not wake the sync worker", exc_info=True)

    # ---- sources: reads ---------------------------------------------------------------------------

    async def list_sources(self, user: UserContext | None = None) -> list[SourceOut]:
        """Every source that is not deleted; the secret's hint only for a Super Admin (or no user)."""
        rows = await self.repo.list_sources()
        if not rows:
            return []
        counts = await self.repo.counts_by_source()
        active = await self.repo.active_runs()
        box = self._box_or_none()
        return [
            source_to_out(
                r, box=box, active_run=active.get(int(r["id"])), counts=counts.get(int(r["id"])),
                show_secret_hint=_sees_secret_hint(user),
            )
            for r in rows
        ]

    async def get_source_out(self, source_id: int, user: UserContext | None = None) -> SourceOut:
        row = await self._require_source(source_id)
        counts = (await self.repo.counts_by_source(source_id)).get(int(source_id))
        active = (await self.repo.active_runs(source_id)).get(int(source_id))
        return source_to_out(
            row, box=self._box_or_none(), active_run=active, counts=counts, show_secret_hint=_sees_secret_hint(user)
        )

    # ---- sources: admin actions -------------------------------------------------------------------

    async def create_source(self, user: UserContext, body: SourceCreate) -> SourceOut:
        name = clean_name(body.name)
        account_id = await self._check_account(body.account_id)
        site_url = self._site_url(body.site_url)
        drive_name = clean_drive_name(body.drive_name)
        folder_path = clean_folder_path(body.folder_path)
        extensions = normalize_extensions(body.file_extensions)
        tenant_id = clean_tenant_id(body.tenant_id)
        client_id = clean_client_id(body.client_id)
        secret = clean_secret(body.client_secret)

        box = self._require_box()
        tenant_enc = self._encrypt(box, tenant_id, MAX_TENANT_ENC_BYTES, "Tenant ID")
        client_enc = self._encrypt(box, client_id, MAX_CLIENT_ENC_BYTES, "Client ID")
        secret_enc = self._encrypt(box, secret, MAX_SECRET_ENC_BYTES, "Client secret")
        hint = self._secret_hint(secret)
        now = utc_now()
        next_at = schedule.next_sync_at(
            enabled=body.sync_enabled,
            interval_days=body.sync_interval_days,
            hour=body.sync_hour,
            tz_name=self.settings.timezone,
            now_utc=now,
        )
        source_id = await self.repo.insert_source(
            name=name,
            account_id=account_id,
            tenant_id_enc=tenant_enc,
            client_id_enc=client_enc,
            client_secret_enc=secret_enc,
            client_secret_hint=hint,
            secret_updated_at=now,
            site_url=site_url,
            drive_name=drive_name,
            folder_path=folder_path,
            recursive=body.recursive,
            file_extensions=extensions,
            sync_enabled=body.sync_enabled,
            sync_interval_days=body.sync_interval_days,
            sync_hour=body.sync_hour,
            next_sync_at=next_at,
            created_by=user.id,
        )
        await self._audit_safe(
            user,
            source_id,
            "CREATE",
            new={
                "name": name,
                "account_id": account_id,
                "tenant_id": mask(tenant_id),
                "client_id": mask(client_id),
                "client_secret": "set",
                "site_url": site_url,
                "drive_name": drive_name,
                "folder_path": folder_path,
                "recursive": body.recursive,
                "file_extensions": extensions,
                "sync_enabled": body.sync_enabled,
                "sync_interval_days": body.sync_interval_days,
                "sync_hour": body.sync_hour,
            },
        )
        _log.info("doc_intel: SharePoint source %s created", source_id)
        return await self.get_source_out(source_id, user)

    async def update_source(self, user: UserContext, source_id: int, body: SourceUpdate) -> SourceOut:
        """Only the fields present are changed. Omitting the secret keeps it; changing the site,
        library, folder or tenant forgets the resolved ids; a schedule change recomputes next_sync_at."""
        row = await self._require_source(source_id)
        given = body.model_fields_set
        columns: dict[str, Any] = {}
        old: dict[str, Any] = {}
        new: dict[str, Any] = {}

        def change(column: str, value: Any, key: str | None = None, *, shown_old: Any = None, shown_new: Any = None) -> None:
            label = key or column
            columns[column] = value
            old[label] = row.get(column) if shown_old is None else shown_old
            new[label] = value if shown_new is None else shown_new

        if body.name is not None:
            name = clean_name(body.name)
            if name != row.get("name"):
                change("name", name)
        if "account_id" in given:
            account_id = await self._check_account(body.account_id)
            if account_id != _int(row.get("account_id")):
                change("account_id", account_id)
        location_changed = False
        if body.site_url is not None:
            site_url = self._site_url(body.site_url)
            if site_url != row.get("site_url"):
                change("site_url", site_url)
                location_changed = True
        if "drive_name" in given:
            drive_name = clean_drive_name(body.drive_name)
            if drive_name != (row.get("drive_name") or None):
                change("drive_name", drive_name)
                location_changed = True
        if "folder_path" in given:
            folder_path = clean_folder_path(body.folder_path)
            if folder_path != (row.get("folder_path") or None):
                change("folder_path", folder_path)
                location_changed = True
        if body.recursive is not None and body.recursive != bool(_int(row.get("recursive"))):
            change("recursive", int(body.recursive), shown_old=bool(_int(row.get("recursive"))), shown_new=body.recursive)
        if body.file_extensions is not None:
            extensions = normalize_extensions(body.file_extensions)
            if extensions != extensions_from_column(row.get("file_extensions")):
                change(
                    "file_extensions",
                    ",".join(extensions),
                    shown_old=extensions_from_column(row.get("file_extensions")),
                    shown_new=extensions,
                )

        tenant = clean_tenant_id(body.tenant_id) if body.tenant_id is not None else None
        client = clean_client_id(body.client_id) if body.client_id is not None else None
        secret = clean_secret(body.client_secret) if body.client_secret is not None else None
        if tenant is not None or client is not None or secret is not None:
            box = self._require_box()
            stored_tenant = self._decrypt_or_none(box, row.get("tenant_id_enc"))
            stored_client = self._decrypt_or_none(box, row.get("client_id_enc"))
            if tenant is not None and tenant != stored_tenant:
                enc = self._encrypt(box, tenant, MAX_TENANT_ENC_BYTES, "Tenant ID")
                change("tenant_id_enc", enc, "tenant_id", shown_old=mask(stored_tenant) or "unreadable", shown_new=mask(tenant))
                location_changed = True
            if client is not None and client != stored_client:
                enc = self._encrypt(box, client, MAX_CLIENT_ENC_BYTES, "Client ID")
                change("client_id_enc", enc, "client_id", shown_old=mask(stored_client) or "unreadable", shown_new=mask(client))
            if secret is not None:
                enc = self._encrypt(box, secret, MAX_SECRET_ENC_BYTES, "Client secret")
                change("client_secret_enc", enc, "client_secret", shown_old="set", shown_new="rotated")
                columns["client_secret_hint"] = self._secret_hint(secret)
                columns["secret_updated_at"] = utc_now()
            if {"tenant_id_enc", "client_id_enc", "client_secret_enc"} & columns.keys():
                await self._reencrypt_kept_credentials(box, source_id, columns, stored_tenant, stored_client)
        if location_changed:
            columns.update(resolved_site_id=None, resolved_drive_id=None, resolved_folder_id=None)

        old_schedule = (
            bool(_int(row.get("sync_enabled"))),
            _int(row.get("sync_interval_days")) or 14,
            _int(row.get("sync_hour")) if _int(row.get("sync_hour")) is not None else 2,
            str(row.get("status") or SOURCE_ACTIVE),
        )
        new_schedule = (
            body.sync_enabled if body.sync_enabled is not None else old_schedule[0],
            body.sync_interval_days if body.sync_interval_days is not None else old_schedule[1],
            body.sync_hour if body.sync_hour is not None else old_schedule[2],
            body.status if body.status is not None else old_schedule[3],
        )
        if new_schedule != old_schedule:
            for column, index in (("sync_enabled", 0), ("sync_interval_days", 1), ("sync_hour", 2), ("status", 3)):
                if new_schedule[index] != old_schedule[index]:
                    value = int(new_schedule[index]) if column == "sync_enabled" else new_schedule[index]
                    change(column, value, shown_old=old_schedule[index], shown_new=new_schedule[index])
            enabled, interval, hour, status = new_schedule
            columns["next_sync_at"] = schedule.next_sync_at(
                enabled=bool(enabled) and status == SOURCE_ACTIVE,
                interval_days=int(interval),
                hour=int(hour),
                tz_name=self.settings.timezone,
                now_utc=utc_now(),
                last_sync_utc=parse_utc(row.get("last_sync_at")),
            )

        if not columns:
            return await self.get_source_out(source_id, user)
        if not await self.repo.update_source(source_id, columns, updated_by=user.id):
            raise NotFoundError(SOURCE_NOT_FOUND)
        await self._audit_safe(user, source_id, "UPDATE", old=old, new=new)
        return await self.get_source_out(source_id, user)

    async def delete_source(self, user: UserContext, source_id: int) -> None:
        """Soft delete (plan §4): the source becomes DELETED and its ACTIVE entities WITHDRAWN
        (kept, with withdrawn_at, and listed under the deleted source); runs and files stay as
        history, the schedule stops and the stored secret is wiped. 409 while a sync is queued
        or running."""
        row = await self._require_source(source_id)
        try:
            withdrawn = await self.repo.soft_delete_source(source_id, updated_by=user.id)
        except RunAlreadyActive:
            raise ConflictError(SYNC_BLOCKS_DELETE) from None
        if withdrawn is None:
            raise NotFoundError(SOURCE_NOT_FOUND)
        await self._audit_safe(
            user,
            source_id,
            "DELETE",
            old={"name": row.get("name"), "status": row.get("status")},
            new={"status": SOURCE_DELETED, "entities_withdrawn": withdrawn},
        )
        _log.info("doc_intel: SharePoint source %s deleted (%s entities withdrawn)", source_id, withdrawn)

    # ---- diagnostics ------------------------------------------------------------------------------

    async def test_connection(self, source_id: int) -> ConnectionTestOut:
        """Step-by-step diagnostics (read-only): credentials, token, site, drive, folder, listing
        (the steps of ``GraphSource.test_connection``). Never 500s for a connection problem;
        credentials that cannot be decrypted (key missing, rotated away) are a failed
        "credentials" step."""
        source = await self._require_source(source_id)
        checked_at = iso_utc(utc_now())
        started = time.perf_counter()
        try:
            creds = await self._credentials(int(source_id))
        except SecretsUnavailable as ex:
            step = ConnectionStepOut(
                key="credentials",
                label="Credentials",
                ok=False,
                latency_ms=_ms(started),
                detail=ex.reason,
                suggested_action=secrets_action(ex),
            )
            return ConnectionTestOut(ok=False, steps=[step], checked_at=checked_at)
        secret = creds.client_secret
        steps: list[ConnectionStepOut] = []
        extensions = tuple(extensions_from_column(source.get("file_extensions"))) or SUPPORTED_EXTENSIONS

        def run() -> dict[str, Any]:
            graph = self._graph_factory(creds)
            try:
                return graph.test_connection(
                    source.get("site_url"),
                    source.get("drive_name"),
                    source.get("folder_path"),
                    recursive=bool(_int(source.get("recursive"))),
                    extensions=extensions,
                )
            finally:
                _close_quietly(graph)

        try:
            raw = await asyncio.wait_for(run_blocking(run), CONNECTION_TEST_TIMEOUT_SECONDS)
        except TimeoutError:
            steps.append(
                ConnectionStepOut(
                    key="timeout",
                    label="Connection test",
                    ok=False,
                    detail=f"The connection test did not finish within {CONNECTION_TEST_TIMEOUT_SECONDS:g} s",
                    suggested_action="Allow outbound HTTPS to login.microsoftonline.com and graph.microsoft.com, then test again",
                )
            )
            return ConnectionTestOut(ok=False, steps=steps, checked_at=checked_at)
        except GraphFailed as ex:
            steps.append(
                ConnectionStepOut(
                    key="token",
                    label="Microsoft sign-in",
                    ok=False,
                    detail=scrub_secrets(ex.reason, secret),
                    suggested_action=scrub_secrets(ex.suggested_action, secret) if ex.suggested_action else None,
                )
            )
            return ConnectionTestOut(ok=False, steps=steps, checked_at=checked_at)
        except Exception as ex:
            _log.warning("doc_intel: connection test of source %s raised %s", source_id, type(ex).__name__)
            steps.append(
                ConnectionStepOut(
                    key="error",
                    label="Connection test",
                    ok=False,
                    detail=f"Unexpected error: {type(ex).__name__}: {scrub_secrets(_first_line(ex), secret)}",
                )
            )
            return ConnectionTestOut(ok=False, steps=steps, checked_at=checked_at)
        return _connection_out(raw if isinstance(raw, Mapping) else {}, steps, checked_at, secret)

    # ---- "Sync now", retry and history -------------------------------------------------------------

    async def sync_now(self, user: UserContext, source_id: int) -> SyncRunOut:
        """Queue a MANUAL run (409 while one is QUEUED or RUNNING) and wake the worker."""
        source = await self._require_source(source_id)
        if source.get("status") != SOURCE_ACTIVE:
            raise ConflictError(SOURCE_DISABLED_SYNC)
        try:
            run_id = await self.repo.create_run(source_id, trigger_type=TRIGGER_MANUAL, triggered_by=user.id)
        except RunAlreadyActive:
            raise ConflictError(SYNC_ALREADY_ACTIVE) from None
        self.wake_worker()
        await self._audit_safe(user, source_id, "SYNC_NOW", new={"run_id": run_id})
        run = await self.repo.get_run(run_id)
        if run is None:
            raise NotFoundError("Sync run not found")
        return run_to_out(run)

    async def retry_file(self, user: UserContext, file_id: int) -> SourceFileOut:
        """A failed file goes back to PENDING (fresh retry budget) and a MANUAL run is queued.
        When a run is already QUEUED or RUNNING, the file just stays reset: the queued run, or
        the next one after the running run, processes it."""
        row = await self.repo.get_file(file_id)
        if row is None:
            raise NotFoundError("File not found")
        source_id = int(row["source_id"])
        source = await self.repo.get_source(source_id)
        if source is None:
            raise ConflictError("The file's source was deleted")
        if source.get("status") != SOURCE_ACTIVE:
            raise ConflictError("The source is disabled — enable it before retrying its files")
        if row.get("state") != FILE_ACTIVE:
            raise ConflictError("The file was deleted from SharePoint; it can no longer be retried")
        status = str(row.get("status"))
        if status == FILE_PROCESSING:
            raise ConflictError("The file is being processed right now")
        if status not in (FILE_FAILED, FILE_PENDING):
            raise ConflictError(f"Only failed files can be retried (this one is {status})")
        if status == FILE_FAILED and not await self.repo.reset_file_for_retry(file_id):
            raise ConflictError(_STATE_CHANGED)
        try:
            run_id: int | None = await self.repo.create_run(source_id, trigger_type=TRIGGER_MANUAL, triggered_by=user.id)
        except RunAlreadyActive:
            run_id = None  # a queued run picks the file up; after a running one, the next run does
        self.wake_worker()
        await self._audit_safe(
            user,
            file_id,
            "RETRY",
            old={"status": status, "failed_stage": row.get("failed_stage")},
            new={"status": FILE_PENDING, "run_id": run_id},
            entity_type="crm_source_file",
        )
        fresh = await self.repo.get_file(file_id)
        return file_to_out(fresh or row)

    async def list_runs(self, source_id: int, *, limit: int = 20, offset: int = 0) -> SyncRunListOut:
        await self._require_source(source_id)
        rows, total = await self.repo.list_runs(source_id, limit=limit, offset=offset)
        return SyncRunListOut(items=[run_to_out(r) for r in rows], limit=limit, offset=offset, total=total)

    async def list_files(
        self,
        source_id: int,
        *,
        state: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SourceFileListOut:
        await self._require_source(source_id)
        rows, total = await self.repo.list_files(source_id, state=state, status=status, limit=limit, offset=offset)
        return SourceFileListOut(items=[file_to_out(r) for r in rows], limit=limit, offset=offset, total=total)

    # ---- CRM store ----------------------------------------------------------------------------------

    async def list_entities(
        self,
        *,
        source_id: int | None = None,
        entity_type: str | None = None,
        status: str | None = None,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CrmEntityListOut:
        rows, total = await self.repo.list_entities(
            source_id=source_id, entity_type=entity_type, status=status, q=q, limit=limit, offset=offset
        )
        return CrmEntityListOut(items=[entity_to_out(r) for r in rows], limit=limit, offset=offset, total=total)

    async def get_entity(self, entity_id: int) -> CrmEntityOut:
        row = await self.repo.get_entity(entity_id)
        if row is None:
            raise NotFoundError("Entity not found")
        return entity_to_out(row)

    # ---- one sync run (worker) ------------------------------------------------------------------------

    async def process_run(self, run: Mapping[str, Any], *, should_stop: Callable[[], bool] | None = None) -> str:
        """Process a claimed (RUNNING) run; returns its final status.

        Never raises (except on cancellation): expected failures end the run FAILED with an
        admin-readable reason; anything else is recorded as "Unexpected error".
        """
        ctx = _RunContext(
            run_id=int(run["id"]),
            source_id=int(run["source_id"]),
            worker_id=str(run.get("worker_id") or ""),
            started_at=parse_utc(run.get("started_at")) or utc_now(),
        )
        try:
            return await self._sync(ctx, should_stop or (lambda: False))
        except _RunAbandoned:
            _log.warning("doc_intel: sync run %s is no longer RUNNING for this worker; abandoned", ctx.run_id)
            return "ABANDONED"
        except _StopRequested:
            _log.info("doc_intel: sync run %s left for the shutdown to release", ctx.run_id)
            return "STOPPED"
        except Exception as ex:
            _log.error(
                "doc_intel: sync run %s failed unexpectedly:\n%s",
                ctx.run_id,
                ctx.scrub("".join(traceback.format_exception(type(ex), ex, ex.__traceback__))),
            )
            reason = f"Unexpected error: {type(ex).__name__}: {ctx.scrub(_first_line(ex))}"
            try:
                await self._finish(ctx, RUN_FAILED, reason)
            except Exception:
                _log.warning("doc_intel: could not record the failure of sync run %s", ctx.run_id, exc_info=False)
            await self._error_log_safe(ex, ctx)
            return RUN_FAILED

    async def _sync(self, ctx: _RunContext, should_stop: Callable[[], bool]) -> str:
        source = await self.repo.get_source(ctx.source_id, include_deleted=True)
        if source is None or source.get("status") == SOURCE_DELETED:
            return await self._finish(ctx, RUN_FAILED, SOURCE_DELETED_REASON)
        ctx.source = dict(source)
        if source.get("status") != SOURCE_ACTIVE:
            return await self._finish(ctx, RUN_FAILED, "The source is disabled — enable it, then use Sync now")
        extensions = tuple(extensions_from_column(source.get("file_extensions"))) or SUPPORTED_EXTENSIONS

        # 1. credentials
        try:
            creds = await self._credentials(ctx.source_id)
        except SecretsUnavailable as ex:
            return await self._finish(
                ctx, RUN_FAILED, ex.reason, failed_step="credentials", suggested_action=secrets_action(ex), code=ex.code
            )
        ctx.secret = creds.client_secret
        try:
            graph = self._graph_factory(creds)
        except GraphFailed as ex:
            return await self._graph_failure(ctx, "token", ex)
        try:
            return await self._sync_with(ctx, graph, extensions, should_stop)
        finally:
            _close_quietly(graph)

    async def _sync_with(
        self, ctx: _RunContext, graph: Any, extensions: tuple[str, ...], should_stop: Callable[[], bool]
    ) -> str:
        # 2. sign-in
        try:
            await run_blocking(graph.acquire_token)
        except GraphFailed as ex:
            return await self._graph_failure(ctx, "token", ex)

        # 3. site / library / folder (ids cached on the source) and 4. the listing
        target = _cached_target(ctx.source)
        cached = target is not None
        if target is None:
            try:
                target = await self._resolve(ctx, graph)
            except GraphFailed as ex:
                return await self._graph_failure(ctx, "resolve", ex)
        try:
            listing = await self._list(graph, target, ctx, extensions)
        except GraphFailed as ex:
            if not (cached and ex.code == "not_found"):
                return await self._graph_failure(ctx, "listing", ex)
            # The cached folder is gone (moved or re-created): resolve it again, once.
            _log.info("doc_intel: cached folder of source %s not found; resolving again", ctx.source_id)
            try:
                target = await self._resolve(ctx, graph)
            except GraphFailed as again:
                return await self._graph_failure(ctx, "resolve", again)
            try:
                listing = await self._list(graph, target, ctx, extensions)
            except GraphFailed as again:
                return await self._graph_failure(ctx, "listing", again)

        # 5. diff
        ctx.details.update(listing_complete=bool(listing.complete), truncated_reason=listing.truncated_reason)
        plan = plan_sync(await self.repo.active_files(ctx.source_id), listing.files, complete=bool(listing.complete))
        ctx.counts.update(
            files_seen=plan.seen,
            files_new=len(plan.new),
            files_changed=len(plan.changed),
            files_unchanged=len(plan.unchanged),
        )
        ctx.details.update(files_retried=len(plan.retry), files_ignored=plan.ignored)

        # 6. deletions, only after a complete listing
        if listing.complete and plan.deleted:
            ctx.counts["files_deleted"] = await self.repo.mark_files_deleted(plan.deleted)
        for file_id, remote in plan.renamed:
            await self.repo.refresh_file_metadata(file_id, remote)
        if plan.unchanged:
            await self.repo.mark_seen([file_id for file_id, _ in plan.unchanged])

        work: list[tuple[int, RemoteFile]] = []
        for remote in plan.new:
            work.append((await self.repo.upsert_file(ctx.source_id, remote, run_id=ctx.run_id, reset_attempts=True), remote))
        for _, remote in plan.changed:
            work.append((await self.repo.upsert_file(ctx.source_id, remote, run_id=ctx.run_id, reset_attempts=True), remote))
        for _, remote in plan.retry:
            work.append((await self.repo.upsert_file(ctx.source_id, remote, run_id=ctx.run_id, reset_attempts=False), remote))
        ctx.details.update(files_to_process=len(work), files_processed=0)
        await self._progress(ctx)

        # 7. the files, one at a time
        for index, (file_id, remote) in enumerate(work):
            if should_stop():
                raise _StopRequested()
            state = await self._source_status(ctx.source_id)
            if state != SOURCE_ACTIVE:
                gone = "deleted" if state in (None, SOURCE_DELETED) else "disabled"
                return await self._finish(ctx, RUN_FAILED, f"Stopped: the source was {gone} during the sync")
            if not await self._process_file(ctx, graph, file_id, remote):
                ctx.counts["files_failed"] += 1
            ctx.details["files_processed"] = index + 1
            await self._progress(ctx)

        # 8. outcome
        problems: list[str] = []
        failed = ctx.counts["files_failed"]
        if failed:
            problems.append(f"{failed} of {len(work)} file{'s' if len(work) != 1 else ''} failed")
        if not listing.complete:
            why = f" ({listing.truncated_reason})" if listing.truncated_reason else ""
            problems.append(f"the folder listing was incomplete{why}, so deleted files were not detected")
        if problems:
            text = "; ".join(problems)
            return await self._finish(ctx, RUN_PARTIAL, text[:1].upper() + text[1:])
        return await self._finish(ctx, RUN_COMPLETED, None)

    async def _process_file(self, ctx: _RunContext, graph: Any, file_id: int, remote: RemoteFile) -> bool:
        """One file through the five stages; True when COMPLETED. A file-level problem marks its
        stage FAILED with a reason (never raises for it); the private temp dir is always removed."""
        if not await self.repo.begin_file(file_id, ctx.run_id):
            _log.warning("doc_intel: file %s of source %s could not be started; skipped", file_id, ctx.source_id)
            return False
        ctx.stage = CRM_STAGES[0]
        work_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(self.temp_root) if self.temp_root else None))
        try:
            return await self._file_stages(ctx, graph, file_id, remote, work_dir)
        except (_RunAbandoned, _StopRequested):
            raise
        except Exception as ex:
            _log.error(
                "doc_intel: file %s failed unexpectedly at %s:\n%s",
                file_id,
                ctx.stage,
                ctx.scrub("".join(traceback.format_exception(type(ex), ex, ex.__traceback__))),
            )
            reason = f"Unexpected error: {type(ex).__name__}: {ctx.scrub(_first_line(ex))}"
            await self._fail_file(ctx, file_id, ctx.stage, reason)
            await self._error_log_safe(ex, ctx)
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _file_stages(self, ctx: _RunContext, graph: Any, file_id: int, remote: RemoteFile, work_dir: Path) -> bool:
        # download (size capped, into the private temp dir)
        ctx.stage = "download"
        await self._stage(file_id, "download", STAGE_RUNNING)
        limit = self.settings.sync_max_file_bytes
        if remote.size is not None and remote.size > limit:
            return await self._fail_file(
                ctx,
                file_id,
                "download",
                f"File is {_mb(remote.size)} MB; the limit is {self.settings.sync_max_file_mb} MB (DOC_INTEL_SYNC_MAX_FILE_MB)",
            )
        ext = Path(remote.name or "").suffix.lower()
        media_type = ALLOWED_EXTENSIONS.get(ext)
        if media_type is None:
            return await self._fail_file(
                ctx, file_id, "download", f"Unsupported file type (only {', '.join(SUPPORTED_EXTENSIONS)} are processed)"
            )
        target = work_dir / f"original{ext}"
        try:
            sha256 = await run_blocking(graph.download, remote, target, max_bytes=limit)
        except GraphFailed as ex:
            return await self._fail_file(ctx, file_id, "download", ex.reason, metrics=_failure_metrics(ex, ctx))
        size = target.stat().st_size if target.is_file() else remote.size
        await self._stage(file_id, "download", STAGE_COMPLETED, metrics={"size_bytes": size})

        # extraction + intelligence + entities: one child process
        ctx.stage = "extraction"
        await self._stage(file_id, "extraction", STAGE_RUNNING)
        job_dir = work_dir / "job"
        job_dir.mkdir()
        try:
            result = await run_blocking(
                self._extract,
                target,
                filename=remote.name,
                media_type=media_type,
                sha256=str(sha256 or ""),
                work_dir=job_dir,
                settings=self.settings,
                use_intelligence=bool(_int(ctx.source.get("use_intelligence"))),
            )
        except CrmExtractionFailed as ex:
            stage = ex.stage if ex.stage in _CHILD_STAGES else "extraction"
            earlier = _CHILD_STAGES[: _CHILD_STAGES.index(stage)]
            if ex.code in _NOT_STARTED_CODES:
                # Refused before the document was extracted: nothing before ``stage`` ran.
                return await self._fail_file(
                    ctx, file_id, stage, ex.reason, pending=earlier, metrics=_failure_metrics(ex, ctx)
                )
            # The child reached ``stage`` (it reports the stage it was in), so the earlier ones completed.
            return await self._fail_file(
                ctx, file_id, stage, ex.reason, completed=earlier, metrics=_failure_metrics(ex, ctx)
            )
        metrics = dict(result.metrics or {})
        seconds = metrics.get("seconds") if isinstance(metrics.get("seconds"), Mapping) else {}
        entities = list(result.entities or [])
        issues = [dict(i) for i in (result.issues or []) if isinstance(i, Mapping)]
        errors = sum(1 for i in issues if str(i.get("severity")) == "error")
        await self._stage(
            file_id, "extraction", STAGE_COMPLETED, metrics={**_extraction_metrics(result), **_child_seconds(seconds, "extraction")}
        )
        intelligence: dict[str, Any] = {
            "entities": len(entities),
            "use_intelligence": bool(_int(ctx.source.get("use_intelligence"))),
            **_child_seconds(seconds, "intelligence"),
        }
        for key in ("document_type", "entity_types", "extractors"):
            if metrics.get(key) is not None:
                intelligence[key] = metrics[key]
        await self._stage(file_id, "intelligence", STAGE_COMPLETED, metrics=intelligence)
        await self._stage(
            file_id,
            "entities",
            STAGE_COMPLETED,
            metrics={"valid": bool(result.valid), "errors": errors, "warnings": len(issues) - errors, **_child_seconds(seconds, "entities")},
        )

        # persist: entities replaced and the file completed in one transaction
        ctx.stage = "persist"
        await self._stage(file_id, "persist", STAGE_RUNNING)
        display_fields, field_types = schema_hints(metrics)
        rows = build_entity_rows(entities, issues, display_fields=display_fields, field_types=field_types)
        warnings = [w.model_dump() for w in result.normalized.warnings][:200] if result.normalized is not None else []
        if errors:
            warnings.append(
                {
                    "code": "invalid_entities",
                    "message": f"{errors} validation error{'s' if errors != 1 else ''} in the extracted entities",
                    "page": None,
                }
            )
        try:
            ok = await self.repo.complete_file(
                file_id,
                source_id=ctx.source_id,
                account_id=_int(ctx.source.get("account_id")),
                entities=rows,
                result=result.result,
                warnings=warnings,
                is_valid=bool(result.valid),
                content_sha256=str(sha256) if sha256 else None,
                metrics={"entities_written": len(rows)},
            )
        except Exception as ex:
            _log.warning("doc_intel: persisting file %s failed (%s)", file_id, type(ex).__name__)
            return await self._fail_file(
                ctx, file_id, "persist", f"Could not store the entities in the CRM store: {ctx.scrub(_first_line(ex))}"
            )
        if not ok:
            raise _RunAbandoned()
        return True

    # ---- run helpers -----------------------------------------------------------------------------------

    async def _resolve(self, ctx: _RunContext, graph: Any) -> ResolvedTarget:
        source = ctx.source
        target = await run_blocking(graph.resolve, source.get("site_url"), source.get("drive_name"), source.get("folder_path"))
        try:
            await self.repo.set_resolved(
                ctx.source_id,
                target,
                site_url=source.get("site_url"),
                drive_name=source.get("drive_name"),
                folder_path=source.get("folder_path"),
            )
        except Exception:
            _log.warning("doc_intel: could not cache the resolved ids of source %s", ctx.source_id, exc_info=True)
        return target

    async def _list(self, graph: Any, target: ResolvedTarget, ctx: _RunContext, extensions: tuple[str, ...]) -> ListingResult:
        return await run_blocking(
            graph.list_files,
            target,
            recursive=bool(_int(ctx.source.get("recursive"))),
            extensions=extensions,
            max_files=self.settings.sync_max_files,
        )

    async def _stage(self, file_id: int, stage: str, status: str, *, metrics: Mapping[str, Any] | None = None) -> None:
        if not await self.repo.set_file_stage(file_id, stage, status, metrics=metrics):
            raise _RunAbandoned()

    async def _fail_file(
        self,
        ctx: _RunContext,
        file_id: int,
        stage: str,
        reason: str,
        *,
        completed: Sequence[str] = (),
        pending: Sequence[str] = (),
        metrics: Mapping[str, Any] | None = None,
    ) -> bool:
        reason = truncate_utf8(ctx.scrub(reason) or "Failed", MAX_ERROR_BYTES) or "Failed"
        _log.info("doc_intel: file %s of source %s failed at %s: %s", file_id, ctx.source_id, stage, reason)
        if not await self.repo.fail_file(file_id, stage, reason, completed=completed, pending=pending, metrics=metrics):
            raise _RunAbandoned()
        return False

    async def _progress(self, ctx: _RunContext) -> None:
        if not await self.repo.set_run_progress(ctx.run_id, worker_id=ctx.worker_id, counts=ctx.counts, details=ctx.details):
            raise _RunAbandoned()

    async def _source_status(self, source_id: int) -> str | None:
        row = await self.repo.get_source(source_id, include_deleted=True)
        return str(row.get("status")) if row else None

    async def _graph_failure(self, ctx: _RunContext, step: str, ex: GraphFailed) -> str:
        return await self._finish(
            ctx, RUN_FAILED, ex.reason, failed_step=step, suggested_action=ex.suggested_action, code=ex.code
        )

    async def _finish(
        self,
        ctx: _RunContext,
        status: str,
        error: str | None,
        *,
        failed_step: str | None = None,
        suggested_action: str | None = None,
        code: str | None = None,
    ) -> str:
        """End the run and record it on the source: last_sync_*, last_success_at (COMPLETED) and
        next_sync_at = next_after_run(run start) while the schedule is on."""
        now = utc_now()
        details: dict[str, Any] = {**ctx.details, "seconds": round(max(0.0, (now - ctx.started_at).total_seconds()), 3)}
        if failed_step:
            details["failed_step"] = failed_step
        if suggested_action:
            details["suggested_action"] = ctx.scrub(suggested_action)
        if code:
            details["error_code"] = code
        message = truncate_utf8(ctx.scrub(error), MAX_ERROR_BYTES) if error else None
        source = await self.repo.get_source(ctx.source_id, include_deleted=True)
        next_at: datetime | None = None
        interval = hour = None
        if source is not None and source.get("status") != SOURCE_DELETED:
            interval = _int(source.get("sync_interval_days")) or 14
            hour = _int(source.get("sync_hour"))
            hour = 2 if hour is None else hour
            next_at = schedule.next_after_run(ctx.started_at, interval_days=interval, hour=hour, tz_name=self.settings.timezone)
        finished = await self.repo.finish_run(
            ctx.run_id,
            worker_id=ctx.worker_id,
            status=status,
            error_message=message,
            counts=ctx.counts,
            details=details,
            source_id=ctx.source_id,
            next_sync_at=next_at,
            schedule_interval_days=interval,
            schedule_hour=hour,
        )
        if not finished:
            raise _RunAbandoned()
        _log.info(
            "doc_intel: sync run %s of source %s %s (%s new, %s changed, %s deleted, %s failed)",
            ctx.run_id,
            ctx.source_id,
            status,
            ctx.counts["files_new"],
            ctx.counts["files_changed"],
            ctx.counts["files_deleted"],
            ctx.counts["files_failed"],
        )
        return status

    # ---- credentials -------------------------------------------------------------------------------

    async def _credentials(self, source_id: int) -> GraphCredentials:
        """The decrypted credentials of a source. Raises SecretsUnavailable."""
        row = await self.repo.get_credentials(source_id)
        if not row or not all(row.get(k) for k in ("tenant_id_enc", "client_id_enc", "client_secret_enc")):
            return decrypt_credentials(row, None)  # raises: incomplete
        return decrypt_credentials(row, self._box_factory())

    def _box_or_none(self) -> Any:
        try:
            return self._box_factory()
        except Exception:
            _log.debug("doc_intel: credentials cannot be decrypted (encryption key unavailable)")
            return None

    def _require_box(self) -> Any:
        """The SecretBox for a credential write; 503 (with the reason) when the key is missing."""
        try:
            return self._box_factory()
        except SecretsUnavailable as ex:
            raise ServiceUnavailableError(ex.reason) from None
        except Exception as ex:
            _log.warning("doc_intel: the encryption key could not be loaded (%s)", type(ex).__name__)
            raise ServiceUnavailableError(f"Credential encryption is unavailable ({type(ex).__name__})") from None

    @staticmethod
    def _encrypt(box: Any, value: str, max_bytes: int, label: str) -> str:
        try:
            token = box.encrypt(value)
        except SecretsUnavailable as ex:
            raise ServiceUnavailableError(ex.reason) from None
        if not isinstance(token, str) or not fits(token, max_bytes):
            raise BadRequestError(f"{label} is too long")
        return token

    async def _reencrypt_kept_credentials(
        self,
        box: Any,
        source_id: int,
        columns: dict[str, Any],
        stored_tenant: str | None,
        stored_client: str | None,
    ) -> None:
        """A credential write puts every stored credential under the current (first) key: the ones
        kept unchanged are re-encrypted too. Step 2 of the key rotation in crypto.py ("re-save each
        source's credentials") relies on it; without it the old key could never be dropped (review
        finding F25). Values that cannot be decrypted are left as they are (the admin re-enters
        them). Re-encrypting is not a change: nothing is audited and the secret's hint and date stay."""
        if "tenant_id_enc" not in columns and stored_tenant is not None:
            columns["tenant_id_enc"] = self._encrypt(box, stored_tenant, MAX_TENANT_ENC_BYTES, "Tenant ID")
        if "client_id_enc" not in columns and stored_client is not None:
            columns["client_id_enc"] = self._encrypt(box, stored_client, MAX_CLIENT_ENC_BYTES, "Client ID")
        if "client_secret_enc" not in columns:
            stored = await self.repo.get_credentials(int(source_id))
            kept_secret = self._decrypt_or_none(box, (stored or {}).get("client_secret_enc"))
            if kept_secret is not None:
                columns["client_secret_enc"] = self._encrypt(box, kept_secret, MAX_SECRET_ENC_BYTES, "Client secret")

    @staticmethod
    def _decrypt_or_none(box: Any, token: Any) -> str | None:
        if not token:
            return None
        try:
            return box.decrypt(token)
        except Exception:
            return None

    def _secret_hint(self, secret: str) -> str | None:
        try:
            hint = self._hint(secret)
        except Exception:
            _log.warning("doc_intel: no display hint for the client secret")
            return None
        return hint if isinstance(hint, str) and fits(hint, MAX_HINT_BYTES) else None

    # ---- validation helpers --------------------------------------------------------------------------

    async def _require_source(self, source_id: int) -> dict[str, Any]:
        row = await self.repo.get_source(source_id)
        if row is None:
            raise NotFoundError(SOURCE_NOT_FOUND)
        return row

    async def _check_account(self, account_id: int | None) -> int | None:
        if account_id is None:
            return None
        if await self.repo.get_account(int(account_id)) is None:
            raise NotFoundError("Account not found")
        return int(account_id)

    def _site_url(self, value: str) -> str:
        try:
            url = self._validate_site_url(str(value or "").strip(), self.settings)
        except GraphFailed as ex:
            raise BadRequestError(ex.reason) from None
        if not fits(url, MAX_SITE_URL_BYTES):
            raise BadRequestError("Site URL is too long")
        return url

    # ---- audit and error log -------------------------------------------------------------------------

    async def _audit_safe(
        self,
        user: UserContext,
        entity_id: int,
        action: str,
        *,
        old: Any = None,
        new: Any = None,
        entity_type: str = "crm_source",
    ) -> None:
        if not self.settings.audit_enabled:
            return
        try:
            await self._audit(
                user_id=user.id, entity_type=entity_type, entity_id=entity_id, action_type=action, old_value=old, new_value=new
            )
        except Exception:
            _log.warning("doc_intel: audit %s %s %s not written", entity_type, action, entity_id, exc_info=True)

    async def _write_audit(
        self, *, user_id: int | None, entity_type: str, entity_id: int, action_type: str, old_value: Any, new_value: Any
    ) -> None:
        if self._db is None:
            return
        await write_audit_log(
            self._db,
            user_id=user_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action_type=action_type,
            old_value=old_value,
            new_value=new_value,
        )

    async def _error_log_safe(self, ex: BaseException, ctx: _RunContext) -> None:
        if not self.settings.error_log_enabled:
            return
        try:
            await self._error_log(
                exception_type=type(ex).__name__,
                exception_message=truncate_utf8(ctx.scrub(str(ex)), 2000),
                stack_trace=ctx.scrub("".join(traceback.format_exception(type(ex), ex, ex.__traceback__))),
                path=f"/api/doc-intel/sources/{ctx.source_id}/runs",
            )
        except Exception:
            _log.warning("doc_intel: error log not written", exc_info=True)

    async def _write_error_log(
        self, *, exception_type: str, exception_message: str | None, stack_trace: str | None, path: str | None
    ) -> None:
        # Scoped to the platform org, or not written at all (plan review finding F4): rows
        # without an org are visible to every org-scoped viewer of /api/logs/errors.
        org_id = get_backend_settings().notify_platform_org_id
        if org_id is None:
            _log.warning("doc_intel: %s not written to AIVA_error_logs (set NOTIFY_PLATFORM_ORG_ID to scope it)", exception_type)
            return
        await persist_error_log(
            self._db,
            exception_type=exception_type,
            exception_message=exception_message,
            stack_trace=stack_trace,
            source="DOC_INTEL",
            path=path,
            route_template="doc_intel.sync_run",
            org_id=int(org_id),
        )


class SyncWorker:
    """Background loop: recover interrupted runs, claim the next QUEUED run, process it.

    One loop per process, one run at a time. ``stop()`` fails the run in flight as
    "Interrupted by shutdown" (anything it misses is recovered by ``recover_stale_runs``).
    """

    def __init__(
        self,
        service: SyncService,
        settings: DocIntelSettings,
        worker_id: str | None = None,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._service = service
        self._settings = settings
        self.worker_id = (worker_id or default_worker_id())[:128]
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_after = stale_threshold(heartbeat_seconds)
        self._task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._stopping = False
        self._current_run: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the loop on the running event loop (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._wake_event = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="doc-intel-sync-worker")
        _log.info("doc_intel: sync worker %s started", self.worker_id)

    def wake(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    def request_stop(self) -> None:
        """Take no new file or run (shutdown step 0: before extraction children are killed)."""
        self._stopping = True

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel the loop; the run in flight is failed as "Interrupted by shutdown"."""
        self._stopping = True
        task, current = self._task, self._current_run
        self._task = None
        if task is None:
            return
        self.wake()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout)
        except Exception:
            _log.warning("doc_intel: sync worker did not stop within %ss", timeout)
        if current is not None:
            try:
                await asyncio.wait_for(
                    self._service.repo.release_interrupted_run(current, self.worker_id, reason=SHUTDOWN_RUN_REASON), 5
                )
            except Exception:
                _log.warning("doc_intel: could not release sync run %s", current, exc_info=True)

    async def _run(self) -> None:
        try:
            removed = await run_blocking(sweep_stale_temp_dirs, self._service.temp_root)
            if removed:
                _log.info("doc_intel: removed %s leftover sync download director%s", removed, "ies" if removed != 1 else "y")
        except Exception:
            _log.debug("doc_intel: temp sweep failed", exc_info=True)
        repo = self._service.repo
        while not self._stopping:
            try:
                await repo.recover_stale_runs(utc_now() - self._stale_after)
                while not self._stopping:
                    run = await repo.claim_next_run(self.worker_id)
                    if run is None:
                        break
                    await self._process(run)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("doc_intel: sync worker poll failed")
            await self._idle()

    async def _process(self, run: Mapping[str, Any]) -> None:
        run_id = int(run["id"])
        self._current_run = run_id
        heartbeat = asyncio.create_task(self._heartbeat(run_id), name=f"doc-intel-sync-heartbeat-{run_id}")
        outcome: str | None = None
        try:
            outcome = await self._service.process_run(run, should_stop=lambda: self._stopping)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if outcome != "STOPPED":  # a run stopped at a file boundary is released by stop()
                self._current_run = None

    async def _heartbeat(self, run_id: int) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                await self._service.repo.touch_run(run_id, self.worker_id)
            except Exception:
                _log.warning("doc_intel: heartbeat for sync run %s failed", run_id, exc_info=True)

    async def _idle(self) -> None:
        event = self._wake_event
        if event is None:
            await asyncio.sleep(self._settings.worker_poll_seconds)
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=self._settings.worker_poll_seconds)
        except TimeoutError:
            pass
        event.clear()


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:sync-{uuid.uuid4().hex[:8]}"[:128]


def sweep_stale_temp_dirs(root: Path | None = None, *, max_age: timedelta = TEMP_MAX_AGE) -> int:
    """Remove download directories a crash left behind (older than ``max_age``). Never raises."""
    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - max_age.total_seconds()
    removed = 0
    try:
        candidates = list(base.glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


# ---- module helpers ------------------------------------------------------------------------------


def _cached_target(source: Mapping[str, Any]) -> ResolvedTarget | None:
    ids = (source.get("resolved_site_id"), source.get("resolved_drive_id"), source.get("resolved_folder_id"))
    if not all(ids):
        return None
    return ResolvedTarget(site_id=str(ids[0]), drive_id=str(ids[1]), folder_id=str(ids[2]))


def _failure_metrics(ex: GraphFailed | CrmExtractionFailed, ctx: _RunContext) -> dict[str, Any] | None:
    """The failed stage's code and suggested action, kept in its stage_details entry."""
    out: dict[str, Any] = {}
    if getattr(ex, "code", None):
        out["code"] = ex.code
    if getattr(ex, "suggested_action", None):
        out["suggested_action"] = ctx.scrub(ex.suggested_action)
    return out or None


def _child_seconds(seconds: Mapping[str, Any], stage: str) -> dict[str, Any]:
    """The extraction child's own timing of one of its stages (they run in one call)."""
    value = seconds.get(stage)
    return {"child_seconds": value} if isinstance(value, (int, float)) and not isinstance(value, bool) else {}


def _extraction_metrics(result: CrmExtractionResult) -> dict[str, Any]:
    doc = result.normalized
    metrics: dict[str, Any] = {}
    if doc is not None:
        metrics.update(pages=doc.page_count, blocks=len(doc.blocks), text_chars=doc.text_chars, warnings=len(doc.warnings))
        for key in ("name", "version", "mode", "pdf_engine", "fallback", "seconds"):
            value = (doc.extractor or {}).get(key)
            if isinstance(value, (str, int, float, bool)):
                metrics[f"extractor_{key}"] = value
    for key in ("ocr_used", "fallback", "document_extractor_version", "crm_ingestion_version"):
        value = (result.metrics or {}).get(key)
        if isinstance(value, (str, int, float, bool)) and key not in metrics:
            metrics[key] = value
    return metrics


def _connection_out(raw: Mapping[str, Any], steps: list[ConnectionStepOut], checked_at: str | None, secret: str | None) -> ConnectionTestOut:
    """GraphSource.test_connection() output as the API model, every text scrubbed of the secret."""

    def clean(value: Any) -> str | None:
        return scrub_secrets(str(value), secret) if value not in (None, "") else None

    for item in raw.get("steps") or []:
        if not isinstance(item, Mapping):
            continue
        try:
            steps.append(
                ConnectionStepOut(
                    key=str(item.get("key") or "step"),
                    label=str(item.get("label") or item.get("key") or "Step"),
                    ok=bool(item.get("ok")),
                    latency_ms=_int(item.get("latency_ms")),
                    detail=clean(item.get("detail")),
                    suggested_action=clean(item.get("suggested_action")),
                )
            )
        except ValidationError:
            continue
    samples = [clean(name) for name in (raw.get("sample_files") or []) if name][:20]
    return ConnectionTestOut(
        ok=bool(raw.get("ok")) and bool(steps) and all(step.ok for step in steps),
        steps=steps,
        checked_at=iso_utc(raw.get("checked_at")) or checked_at,
        sample_files=[s[:255] for s in samples if s],
        files_found=_int(raw.get("files_found")),
    )
