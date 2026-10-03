"""In-memory fakes for the Phase 2 (SharePoint -> CRM) tests, and the ``crm_env`` fixture.

``FakeCrmRepo`` stands in for ``CrmRepo`` with the same row shapes and state rules (the
conditional updates, the one-QUEUED/RUNNING-run-per-source unique index, soft deletes, the
secret's ciphertext never selected by the listing queries). ``FakeBox``, ``FakeGraph`` and
``FakeCrmExtractor`` stand in for Agent 4's SecretBox, GraphSource and run_crm_extraction.
Nothing here touches a database, Microsoft, the network or the real ``.env``; every
credential is a made-up test value.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.doc_intel.crm_extraction import CrmExtractionResult
from backend.doc_intel.crm_repo import (
    _FILE_WRITABLE,
    _SOURCE_WRITABLE,
    FINISHED_RUN_STATUSES,
    INCREMENT,
    INTERRUPTED_FILE_REASON,
    INTERRUPTED_RUN_REASON,
    MAX_ENTITIES_PER_FILE,
    SHUTDOWN_RUN_REASON,
    EntityRow,
    RunAlreadyActive,
    apply_file_stage_change,
    file_stage_column,
    remote_columns,
    running_file_stage,
)
from backend.doc_intel.crm_sync import SyncService
from backend.doc_intel.crypto import CIPHERTEXT_PREFIX, SecretsUnavailable
from backend.doc_intel.graph_source import GraphFailed, ListingResult, RemoteFile, ResolvedTarget
from backend.doc_intel.kb_repo import NOW, StageChange, dumps_json, loads_json, parse_utc
from backend.doc_intel.normalized import NormalizedBlock, NormalizedDocument, NormalizedPage, NormalizedWarning
from backend.doc_intel.textutil import truncate_utf8, utc_now

from .conftest import ACCOUNT_ID, Env, make_runtime

TENANT_ID = "8f14e45f-ceea-467a-9575-0c4a1b2d3e4f"
CLIENT_ID = "c9f0f895-fb98-4b91-a1c2-3d4e5f60718a"
SECRET = "Qx8~rT3vN.wPz5sKj2Lm-Hb7YcD9eF1gA4uI6oEe"  # a made-up, Entra-style 40-character secret
NEW_SECRET = "Zz9~aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uVwXy"
SITE_URL = "https://contoso.sharepoint.com/sites/Sales"
KEY_MISSING_REASON = (
    "Credential encryption is not configured: set DOC_INTEL_SECRETS_KEY and restart the backend"
)
TARGET = ResolvedTarget(site_id="site-1", drive_id="drive-1", folder_id="folder-1")
PDF = "application/pdf"


def _iso(value: datetime | None) -> str | None:
    """What backend.database returns for a TIMESTAMP column: naive isoformat()."""
    return value.isoformat() if value is not None else None


def _stored(value: Any) -> Any:
    return _iso(value) if isinstance(value, datetime) else value


def _date_bind(value: datetime) -> datetime:
    """A datetime as the database sees it once bound: python-oracledb binds it as DATE, which
    drops fractional seconds (checked on real Oracle by test_integration_oracle_crm)."""
    return value.replace(microsecond=0)


def remote(
    item_id: str,
    name: str | None = None,
    *,
    ctag: str | None = "c1",
    etag: str | None = "e1",
    quick_xor_hash: str | None = "h1",
    size: int | None = 1234,
    path: str = "/CRM",
    drive_id: str = "drive-1",
    modified_at: datetime | None = datetime(2026, 9, 1, 10, 0, 0),
    web_url: str | None = "auto",
) -> RemoteFile:
    name = name or f"{item_id}.pdf"
    return RemoteFile(
        drive_id=drive_id,
        item_id=item_id,
        name=name,
        path=path,
        web_url=f"https://contoso.sharepoint.com/sites/Sales/Shared%20Documents{path}/{name}" if web_url == "auto" else web_url,
        size=size,
        etag=etag,
        ctag=ctag,
        quick_xor_hash=quick_xor_hash,
        modified_at=modified_at,
        mime_type=PDF,
    )


# ---- SecretBox ---------------------------------------------------------------------------------


class FakeBox:
    """Reversible, obviously-not-plaintext "encryption" with the real prefix."""

    def __init__(self) -> None:
        self.broken = False  # decrypt fails (key rotated away)
        self.encrypted: list[str] = []

    def encrypt(self, plaintext: str) -> str:
        self.encrypted.append(plaintext)
        return CIPHERTEXT_PREFIX + base64.urlsafe_b64encode(plaintext[::-1].encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if self.broken or not isinstance(ciphertext, str) or not ciphertext.startswith(CIPHERTEXT_PREFIX):
            raise SecretsUnavailable("A stored credential could not be decrypted", code="decrypt_failed")
        return base64.urlsafe_b64decode(ciphertext[len(CIPHERTEXT_PREFIX):]).decode("utf-8")[::-1]


def fake_hint(secret: str) -> str:
    return "…" + secret[-4:] if len(secret) >= 8 else "…"


def fake_validate_site_url(site_url: str, settings: Any) -> str:
    text = str(site_url or "").strip().rstrip("/")
    if not text.lower().startswith("https://") or ".sharepoint.com" not in text.lower():
        raise GraphFailed(
            "The site must be on SharePoint Online (*.sharepoint.com), for example https://contoso.sharepoint.com/sites/Sales",
            code="invalid_config",
            suggested_action="Check the site URL",
        )
    return text


# ---- GraphSource -------------------------------------------------------------------------------


class FakeGraph:
    """One scripted Graph session (shared by every GraphSource the factory hands out)."""

    def __init__(self) -> None:
        self.files: list[RemoteFile] = []
        self.complete = True
        self.truncated_reason: str | None = None
        self.target = TARGET
        self.token_error: GraphFailed | None = None
        self.resolve_error: GraphFailed | None = None
        self.list_errors: list[GraphFailed] = []  # consumed one per list_files call
        self.download_errors: dict[str, GraphFailed] = {}
        self.contents: dict[str, bytes] = {}
        self.test_result: dict[str, Any] | None = None
        self.calls: list[tuple[Any, ...]] = []
        self.closed = 0
        self.on_download: Any = None  # callable(RemoteFile) run before a download

    def acquire_token(self) -> None:
        self.calls.append(("token",))
        if self.token_error is not None:
            raise self.token_error

    def resolve(self, site_url: str, drive_name: str | None, folder_path: str | None) -> ResolvedTarget:
        self.calls.append(("resolve", site_url, drive_name, folder_path))
        if self.resolve_error is not None:
            raise self.resolve_error
        return self.target

    def list_files(self, target: ResolvedTarget, *, recursive: bool, extensions: tuple[str, ...], max_files: int) -> ListingResult:
        self.calls.append(("list", target, recursive, tuple(extensions), max_files))
        if self.list_errors:
            raise self.list_errors.pop(0)
        return ListingResult(files=list(self.files), complete=self.complete, truncated_reason=self.truncated_reason)

    def download(self, file: RemoteFile, dest: Path, *, max_bytes: int) -> str:
        self.calls.append(("download", file.item_id))
        if self.on_download is not None:
            self.on_download(file)
        if file.item_id in self.download_errors:
            raise self.download_errors[file.item_id]
        data = self.contents.get(file.item_id, b"%PDF-1.7 " + file.item_id.encode())
        if len(data) > max_bytes:
            raise GraphFailed("The file is larger than the limit", code="too_large")
        Path(dest).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_connection(self, site_url, drive_name, folder_path, *, recursive, extensions) -> dict[str, Any]:
        self.calls.append(("test", site_url, drive_name, folder_path, recursive, tuple(extensions)))
        if self.test_result is not None:
            return copy.deepcopy(self.test_result)
        steps = [
            {"key": key, "label": label, "ok": True, "latency_ms": 5, "detail": detail, "suggested_action": None}
            for key, label, detail in (
                ("credentials", "Credentials", "Tenant ID, Client ID and Client Secret are set"),
                ("token", "Microsoft sign-in", "Signed in"),
                ("site", "SharePoint site", "Sales"),
                ("drive", "Document library", "Library 'Documents'"),
                ("folder", "Folder", "Folder /CRM"),
                ("listing", "File listing", "2 matching PDF/DOCX found"),
            )
        ]
        return {"ok": True, "steps": steps, "checked_at": None, "sample_files": ["a.pdf", "b.docx"], "files_found": 2}

    def close(self) -> None:
        self.closed += 1


class FakeGraphFactory:
    def __init__(self, graph: FakeGraph) -> None:
        self.graph = graph
        self.credentials: list[Any] = []
        self.error: Exception | None = None

    def __call__(self, credentials: Any) -> FakeGraph:
        self.credentials.append(credentials)
        if self.error is not None:
            raise self.error
        return self.graph


# ---- CRM extraction child ----------------------------------------------------------------------


def default_entities(filename: str) -> list[dict[str, Any]]:
    stem = Path(filename).stem
    return [
        {
            "full_name": f"Mona  Adel {stem}",
            "email": f"Mona.Adel+{stem}@Example.COM",
            "phone": "+20 100 123 4567",
            "_meta": {
                "entity_type": "contact",
                "confidence": 0.9,
                "source_document_id": stem,
                "extractors": ["pattern"],
                "fields": {
                    "full_name": {"confidence": 0.9, "extractor": "pattern",
                                  "provenance": [{"page": 1, "block_id": "b1", "source_text": "Contact: Mona Adel"}]},
                },
            },
        },
        {
            "name": f"Acme Trading {stem}",
            "tax_id": "123-456-789",
            "_meta": {"entity_type": "organization", "confidence": 0.8, "fields": {}},
        },
    ]


def make_normalized(filename: str, sha256: str = "0" * 64) -> NormalizedDocument:
    return NormalizedDocument(
        filename=filename,
        media_type=PDF,
        sha256=sha256,
        page_count=1,
        blocks=[NormalizedBlock(kind="paragraph", text="Contact: Mona Adel, mona.adel@example.com", pages=[1])],
        pages=[NormalizedPage(number=1, text="Contact: Mona Adel", classification="text")],
        warnings=[NormalizedWarning(code="low_text", message="Page 1 has little text", page=1)],
        extractor={"name": "document-extractor", "version": "0.1.0", "pdf_engine": "pdfium"},
    )


class FakeCrmExtractor:
    """Stands in for crm_extraction.run_crm_extraction (same signature)."""

    def __init__(self) -> None:
        self.errors: dict[str, Exception] = {}  # by filename
        self.entities: dict[str, list[dict[str, Any]]] = {}  # by filename
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[dict[str, Any]] = []
        self.work_dirs: list[Path] = []
        self.started = threading.Event()
        self.release: threading.Event | None = None  # when set: block until released
        self.before: Any = None  # callable(filename) run first (e.g. delete the source mid-run)

    def __call__(
        self,
        source_path: Path,
        *,
        filename: str,
        media_type: str,
        sha256: str,
        work_dir: Path,
        settings: Any,
        use_intelligence: bool = False,
    ) -> CrmExtractionResult:
        self.calls.append(
            {"filename": filename, "media_type": media_type, "sha256": sha256, "use_intelligence": use_intelligence,
             "source_exists": Path(source_path).is_file()}
        )
        self.work_dirs.append(Path(work_dir))
        Path(work_dir, "crm_job.json").write_text("{}", encoding="utf-8")
        if self.before is not None:
            self.before(filename)
        self.started.set()
        if self.release is not None:
            self.release.wait(10)
        if filename in self.errors:
            raise self.errors[filename]
        issues = copy.deepcopy(self.issues.get(filename, []))
        return CrmExtractionResult(
            normalized=make_normalized(filename, sha256),
            entities=copy.deepcopy(self.entities.get(filename, default_entities(filename))),
            result={"extraction": {"document_id": filename}, "validation": {"valid": not issues, "issues": issues}},
            valid=not any(i.get("severity") == "error" for i in issues),
            issues=issues,
            metrics={
                "document_type": "contract",
                "entity_types": {"contact": 1, "organization": 1},
                "schemas": {
                    "contact": {"display_field": "full_name", "field_types": {"full_name": "string", "email": "email", "phone": "phone"}},
                    "organization": {"display_field": "name", "field_types": {"name": "string", "tax_id": "string"}},
                },
                "seconds": {"extraction": 0.4, "intelligence": 0.05, "entities": 0.01, "total": 0.5},
            },
        )


# ---- CrmRepo ------------------------------------------------------------------------------------


class FakeCrmRepo:
    """In-memory CrmRepo: same method signatures, row shapes and conditional-update rules."""

    def __init__(self) -> None:
        self.sources: dict[int, dict[str, Any]] = {}
        self.runs: dict[int, dict[str, Any]] = {}
        self.files: dict[int, dict[str, Any]] = {}
        self.entities: dict[int, dict[str, Any]] = {}
        self.accounts: dict[int, dict[str, Any]] = {ACCOUNT_ID: {"id": ACCOUNT_ID, "name": "Hallan"}}
        self.users: dict[int, str] = {}
        self._ids = {name: itertools.count(1) for name in ("source", "run", "file", "entity")}
        self.raise_on: dict[str, Exception] = {}  # method name -> error raised once
        self.touches: list[int] = []
        self.recover_calls: list[datetime] = []
        self.schedule_claims: list[int] = []  # source ids passed to claim_due_source

    # -- helpers --

    def _maybe_raise(self, name: str) -> None:
        err = self.raise_on.pop(name, None)
        if err is not None:
            raise err

    def _next(self, table: str) -> int:
        return next(self._ids[table])

    def _live(self, source_id: int) -> bool:
        source = self.sources.get(int(source_id))
        return source is not None and source["status"] != "DELETED"

    def _source_view(self, row: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in row.items() if k != "client_secret_enc"}
        out["client_secret_set"] = 1 if row.get("client_secret_enc") else 0
        account = self.accounts.get(row.get("account_id") or -1)
        out["account_name"] = account["name"] if account else None
        return copy.deepcopy(out)

    def _run_view(self, row: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(row)
        out["triggered_by_email"] = self.users.get(row.get("triggered_by") or -1)
        return out

    def _file_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy({k: v for k, v in row.items() if k != "result_json"})

    def _entity_view(self, row: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(row)
        source = self.sources.get(row["source_id"])
        out["source_name"] = source["name"] if source else None
        out["source_status"] = source["status"] if source else None
        file = self.files.get(row["source_file_id"])
        out["file_name"] = file["name"] if file else None
        return out

    # -- accounts --

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        account = self.accounts.get(int(account_id))
        return dict(account) if account else None

    # -- sources --

    async def insert_source(self, **kw: Any) -> int:
        self._maybe_raise("insert_source")
        source_id = self._next("source")
        now = _iso(utc_now())
        row = {
            "id": source_id,
            "name": truncate_utf8(kw["name"], 512),
            "account_id": kw.get("account_id"),
            "provider": "microsoft_graph",
            "tenant_id_enc": kw["tenant_id_enc"],
            "client_id_enc": kw["client_id_enc"],
            "client_secret_enc": kw["client_secret_enc"],
            "client_secret_hint": kw.get("client_secret_hint"),
            "secret_updated_at": _stored(kw.get("secret_updated_at")),
            "site_url": kw["site_url"],
            "drive_name": kw.get("drive_name"),
            "folder_path": kw.get("folder_path"),
            "recursive": int(bool(kw.get("recursive", True))),
            "file_extensions": ",".join(kw.get("file_extensions") or []),
            "use_intelligence": int(bool(kw.get("use_intelligence", False))),
            "resolved_site_id": None,
            "resolved_drive_id": None,
            "resolved_folder_id": None,
            "sync_enabled": int(bool(kw.get("sync_enabled"))),
            "sync_interval_days": int(kw.get("sync_interval_days", 14)),
            "sync_hour": int(kw.get("sync_hour", 2)),
            "next_sync_at": _stored(kw.get("next_sync_at")),
            "last_sync_at": None,
            "last_sync_status": None,
            "last_sync_error": None,
            "last_success_at": None,
            "status": "ACTIVE",
            "created_by": kw.get("created_by"),
            "updated_by": kw.get("created_by"),
            "created_at": now,
            "updated_at": now,
        }
        self.sources[source_id] = row
        return source_id

    def seed_source(self, box: FakeBox, **fields: Any) -> int:
        """A source row as create_source would store it (for tests that start mid-life)."""
        source_id = self._next("source")
        now = _iso(utc_now())
        row = {
            "id": source_id, "name": f"Source {source_id}", "account_id": ACCOUNT_ID, "provider": "microsoft_graph",
            "tenant_id_enc": box.encrypt(TENANT_ID), "client_id_enc": box.encrypt(CLIENT_ID),
            "client_secret_enc": box.encrypt(SECRET), "client_secret_hint": fake_hint(SECRET), "secret_updated_at": now,
            "site_url": SITE_URL, "drive_name": None, "folder_path": "/CRM", "recursive": 1,
            "file_extensions": ".pdf,.docx", "use_intelligence": 0, "resolved_site_id": None,
            "resolved_drive_id": None, "resolved_folder_id": None, "sync_enabled": 0, "sync_interval_days": 14,
            "sync_hour": 2, "next_sync_at": None, "last_sync_at": None, "last_sync_status": None,
            "last_sync_error": None, "last_success_at": None, "status": "ACTIVE", "created_by": 101,
            "updated_by": 101, "created_at": now, "updated_at": now,
        }
        row.update({k: _stored(v) for k, v in fields.items()})
        self.sources[source_id] = row
        return source_id

    async def get_source(self, source_id: int, *, include_deleted: bool = False) -> dict[str, Any] | None:
        self._maybe_raise("get_source")
        row = self.sources.get(int(source_id))
        if row is None or (not include_deleted and row["status"] == "DELETED"):
            return None
        return self._source_view(row)

    async def list_sources(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        self._maybe_raise("list_sources")
        rows = [r for r in self.sources.values() if include_deleted or r["status"] != "DELETED"]
        return [self._source_view(r) for r in sorted(rows, key=lambda r: r["id"])]

    async def get_credentials(self, source_id: int) -> dict[str, Any] | None:
        row = self.sources.get(int(source_id))
        if row is None or row["status"] == "DELETED":
            return None
        return {k: row[k] for k in ("tenant_id_enc", "client_id_enc", "client_secret_enc")}

    async def update_source(self, source_id: int, columns: dict[str, Any], *, updated_by: int | None) -> bool:
        row = self.sources.get(int(source_id))
        for name in columns:
            if name not in _SOURCE_WRITABLE:
                raise ValueError(f"Column not writable: {name!r}")
        if row is None or row["status"] == "DELETED":
            return False
        row.update({k: _stored(v) for k, v in columns.items()})
        row["updated_by"] = updated_by
        row["updated_at"] = _iso(utc_now())
        return True

    async def set_resolved(self, source_id, target, *, site_url, drive_name, folder_path) -> bool:
        row = self.sources.get(int(source_id))
        if row is None or row["status"] == "DELETED":
            return False
        if (row["site_url"], row["drive_name"], row["folder_path"]) != (site_url, drive_name, folder_path):
            return False
        row.update(resolved_site_id=target.site_id, resolved_drive_id=target.drive_id, resolved_folder_id=target.folder_id)
        return True

    async def soft_delete_source(self, source_id: int, *, updated_by: int | None) -> int | None:
        row = self.sources.get(int(source_id))
        if row is None or row["status"] == "DELETED":
            return None
        if any(r["source_id"] == int(source_id) and r["status"] in ("QUEUED", "RUNNING") for r in self.runs.values()):
            raise RunAlreadyActive(int(source_id))
        now = _iso(utc_now())
        row.update(status="DELETED", sync_enabled=0, next_sync_at=None, client_secret_enc=None,
                   client_secret_hint=None, updated_by=updated_by, updated_at=now)
        withdrawn = 0
        for entity in self.entities.values():
            if entity["source_id"] == int(source_id) and entity["status"] == "ACTIVE":
                entity.update(status="WITHDRAWN", withdrawn_at=now, updated_at=now)
                withdrawn += 1
        return withdrawn

    async def due_sources(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        self._maybe_raise("due_sources")
        rows = [
            r for r in self.sources.values()
            if r["status"] == "ACTIVE" and r["sync_enabled"] == 1 and r["next_sync_at"] is not None
            and parse_utc(r["next_sync_at"]) <= _date_bind(now)
        ]
        rows.sort(key=lambda r: (parse_utc(r["next_sync_at"]), r["id"]))
        return [
            {k: r[k] for k in ("id", "name", "next_sync_at", "sync_interval_days", "sync_hour")} for r in rows[:limit]
        ]

    async def claim_due_source(self, source_id: int, *, now: datetime, new: datetime) -> bool:
        if new <= now:
            raise ValueError("claim_due_source() needs a new next_sync_at after now")
        self.schedule_claims.append(int(source_id))
        row = self.sources.get(int(source_id))
        if row is None or row["status"] != "ACTIVE" or row["sync_enabled"] != 1 or row["next_sync_at"] is None:
            return False
        if parse_utc(row["next_sync_at"]) > _date_bind(now):  # no longer due: another process claimed it
            return False
        row["next_sync_at"] = _iso(_date_bind(new))
        return True

    async def restore_next_sync(self, source_id: int, *, expected: datetime, value: datetime) -> bool:
        row = self.sources.get(int(source_id))
        if row is None or row["next_sync_at"] is None or parse_utc(row["next_sync_at"]) != _date_bind(expected):
            return False
        row["next_sync_at"] = _iso(_date_bind(value))
        return True

    # -- runs --

    async def create_run(self, source_id: int, *, trigger_type: str, triggered_by: int | None) -> int:
        self._maybe_raise("create_run")
        if any(r["source_id"] == int(source_id) and r["status"] in ("QUEUED", "RUNNING") for r in self.runs.values()):
            raise RunAlreadyActive(int(source_id))
        run_id = self._next("run")
        now = _iso(utc_now())
        self.runs[run_id] = {
            "id": run_id, "source_id": int(source_id), "trigger_type": trigger_type, "triggered_by": triggered_by,
            "status": "QUEUED", "worker_id": None, "files_seen": 0, "files_new": 0, "files_changed": 0,
            "files_deleted": 0, "files_unchanged": 0, "files_failed": 0, "error_message": None,
            "details_json": None, "created_at": now, "started_at": None, "finished_at": None, "updated_at": now,
        }
        return run_id

    async def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self.runs.get(int(run_id))
        return self._run_view(row) if row else None

    async def list_runs(self, source_id: int, *, limit: int = 20, offset: int = 0):
        rows = sorted((r for r in self.runs.values() if r["source_id"] == int(source_id)), key=lambda r: r["id"], reverse=True)
        return [self._run_view(r) for r in rows[offset: offset + limit]], len(rows)

    async def active_runs(self, source_id: int | None = None) -> dict[int, dict[str, Any]]:
        return {
            r["source_id"]: self._run_view(r)
            for r in self.runs.values()
            if r["status"] in ("QUEUED", "RUNNING") and (source_id is None or r["source_id"] == int(source_id))
        }

    async def claim_next_run(self, worker_id: str) -> dict[str, Any] | None:
        self._maybe_raise("claim_next_run")
        for run_id in sorted(self.runs):
            row = self.runs[run_id]
            if row["status"] == "QUEUED":
                now = _iso(utc_now())
                row.update(status="RUNNING", worker_id=worker_id[:128], started_at=now, updated_at=now)
                return self._run_view(row)
        return None

    async def touch_run(self, run_id: int, worker_id: str) -> None:
        self.touches.append(int(run_id))
        row = self.runs.get(int(run_id))
        if row and row["status"] == "RUNNING" and row["worker_id"] == worker_id[:128]:
            row["updated_at"] = _iso(utc_now())

    async def set_run_progress(self, run_id: int, *, worker_id: str, counts, details=None) -> bool:
        row = self.runs.get(int(run_id))
        if row is None or row["status"] != "RUNNING" or row["worker_id"] != worker_id[:128]:
            return False
        for name, value in counts.items():
            row[name] = int(value)
        if details is not None:
            row["details_json"] = dumps_json(dict(details))
        row["updated_at"] = _iso(utc_now())
        return True

    async def finish_run(self, run_id, *, worker_id, status, error_message, counts, details, source_id,
                         next_sync_at, schedule_interval_days, schedule_hour) -> bool:
        if status not in FINISHED_RUN_STATUSES:
            raise ValueError(status)
        row = self.runs.get(int(run_id))
        if row is None or row["status"] != "RUNNING" or row["worker_id"] != worker_id[:128]:
            return False
        now = _iso(utc_now())
        error = truncate_utf8(error_message, 4000)
        row.update(status=status, error_message=error, details_json=dumps_json(dict(details)), finished_at=now, updated_at=now)
        for name in ("files_seen", "files_new", "files_changed", "files_deleted", "files_unchanged", "files_failed"):
            row[name] = int(counts.get(name, 0) or 0)
        self._record_on_source(source_id, status, error, now, next_sync_at, schedule_interval_days, schedule_hour)
        return True

    def _record_on_source(self, source_id, status, error, now, next_sync_at, interval, hour) -> None:
        source = self.sources.get(int(source_id))
        if source is None or source["status"] == "DELETED":
            return
        source.update(last_sync_at=now, last_sync_status=status, last_sync_error=error)
        if status == "COMPLETED":
            source["last_success_at"] = now
        if next_sync_at is not None and interval is not None and hour is not None:
            if (source["sync_enabled"] == 1 and source["status"] == "ACTIVE"
                    and source["sync_interval_days"] == interval and source["sync_hour"] == hour):
                source["next_sync_at"] = _iso(next_sync_at)

    async def recover_stale_runs(self, older_than: datetime) -> list[int]:
        self.recover_calls.append(older_than)
        out = []
        for run_id, row in list(self.runs.items()):
            if row["status"] == "RUNNING" and parse_utc(row["updated_at"]) < older_than:
                self._fail_interrupted(row, INTERRUPTED_RUN_REASON)
                out.append(run_id)
        return out

    async def release_interrupted_run(self, run_id: int, worker_id: str, *, reason: str = SHUTDOWN_RUN_REASON) -> bool:
        row = self.runs.get(int(run_id))
        if row is None or row["status"] != "RUNNING" or row["worker_id"] != worker_id[:128]:
            return False
        self._fail_interrupted(row, reason)
        return True

    def _fail_interrupted(self, row: dict[str, Any], reason: str) -> None:
        now = _iso(utc_now())
        row.update(status="FAILED", error_message=reason, finished_at=now, updated_at=now)
        for file in self.files.values():
            if file["last_run_id"] == row["id"] and file["status"] == "PROCESSING":
                stage = running_file_stage(file)
                self._update_file(
                    file["id"], [StageChange(stage, "FAILED", error=INTERRUPTED_FILE_REASON)],
                    {"status": "FAILED", "failed_stage": stage, "error_message": INTERRUPTED_FILE_REASON, "processed_at": NOW},
                    ("PROCESSING",),
                )
        self._record_on_source(row["source_id"], "FAILED", reason, now, None, None, None)

    # -- files --

    async def active_files(self, source_id: int) -> list[dict[str, Any]]:
        keys = ("id", "drive_id", "item_id", "name", "path", "web_url", "etag", "ctag", "quick_xor_hash",
                "size_bytes", "modified_at", "status", "attempts")
        return [{k: r[k] for k in keys} for r in self.files.values() if r["source_id"] == int(source_id) and r["state"] == "ACTIVE"]

    async def get_file(self, file_id: int) -> dict[str, Any] | None:
        row = self.files.get(int(file_id))
        return self._file_view(row) if row else None

    async def list_files(self, source_id: int, *, state=None, status=None, limit=50, offset=0):
        rows = [
            r for r in self.files.values()
            if r["source_id"] == int(source_id) and (not state or r["state"] == state) and (not status or r["status"] == status)
        ]
        rows.sort(key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        return [self._file_view(r) for r in rows[offset: offset + limit]], len(rows)

    def _find_file(self, source_id: int, drive_id: str, item_id: str) -> dict[str, Any] | None:
        return next(
            (r for r in self.files.values()
             if (r["source_id"], r["drive_id"], r["item_id"]) == (int(source_id), drive_id, item_id)),
            None,
        )

    async def upsert_file(self, source_id: int, remote_file: RemoteFile, *, run_id: int, reset_attempts: bool = True) -> int:
        self._maybe_raise("upsert_file")
        meta = {k: _stored(v) for k, v in remote_columns(remote_file).items()}
        existing = self._find_file(source_id, remote_file.drive_id, remote_file.item_id)
        if existing is None:
            file_id = self._next("file")
            now = _iso(utc_now())
            row = {
                "id": file_id, "source_id": int(source_id), "drive_id": remote_file.drive_id,
                "item_id": remote_file.item_id, **meta, "content_sha256": None, "state": "ACTIVE",
                "status": "PENDING", "failed_stage": None, "error_message": None, "warnings_json": None,
                "result_json": None, "stage_details": "{}", "entity_count": None, "is_valid": None, "attempts": 0,
                "last_run_id": int(run_id), "first_seen_at": now, "last_seen_at": now, "processed_at": None,
                "deleted_at": None, "updated_at": now,
            }
            for stage in ("download", "extraction", "intelligence", "entities", "persist"):
                row[file_stage_column(stage)] = "PENDING"
            self.files[file_id] = row
            return file_id
        columns: dict[str, Any] = {**meta, "state": "ACTIVE", "deleted_at": None, "status": "PENDING",
                                   "failed_stage": None, "error_message": None, "last_seen_at": NOW}
        if reset_attempts:
            columns["attempts"] = 0
        self._update_file(existing["id"], [StageChange(s, "PENDING") for s in ("download", "extraction", "intelligence", "entities", "persist")], columns)
        return existing["id"]

    async def refresh_file_metadata(self, file_id: int, remote_file: RemoteFile) -> None:
        row = self.files.get(int(file_id))
        if row and row["state"] == "ACTIVE":
            row.update({k: _stored(v) for k, v in remote_columns(remote_file).items()})
            row["last_seen_at"] = _iso(utc_now())

    async def mark_seen(self, file_ids) -> None:
        now = _iso(utc_now())
        for file_id in file_ids:
            if int(file_id) in self.files:
                self.files[int(file_id)]["last_seen_at"] = now

    async def mark_files_deleted(self, file_ids) -> int:
        now = _iso(utc_now())
        marked = 0
        for file_id in dict.fromkeys(int(i) for i in file_ids):
            row = self.files.get(file_id)
            if row is None or row["state"] != "ACTIVE":
                continue
            row.update(state="DELETED", deleted_at=now, updated_at=now)
            marked += 1
            for entity in self.entities.values():
                if entity["source_file_id"] == file_id and entity["status"] == "ACTIVE":
                    entity.update(status="WITHDRAWN", withdrawn_at=now, updated_at=now)
        return marked

    async def begin_file(self, file_id: int, run_id: int) -> bool:
        return self._update_file(
            file_id,
            [StageChange(s, "PENDING") for s in ("download", "extraction", "intelligence", "entities", "persist")],
            {"status": "PROCESSING", "attempts": INCREMENT, "last_run_id": int(run_id), "failed_stage": None, "error_message": None},
            ("PENDING", "FAILED"),
            "ACTIVE",
        )

    async def set_file_stage(self, file_id, stage, status, *, error=None, metrics=None) -> bool:
        return self._update_file(file_id, [StageChange(stage, status, error=error, metrics=metrics)], {}, ("PROCESSING",))

    async def fail_file(self, file_id, stage, reason, *, completed=(), pending=(), metrics=None) -> bool:
        reason = truncate_utf8(reason or "Failed", 4000)
        stages = [StageChange(s, "PENDING") for s in pending if s != stage]
        stages += [StageChange(s, "COMPLETED") for s in completed if s != stage]
        stages.append(StageChange(stage, "FAILED", error=reason, metrics=metrics))
        return self._update_file(
            file_id, stages, {"status": "FAILED", "failed_stage": stage, "error_message": reason, "processed_at": NOW},
            ("PROCESSING",),
        )

    async def complete_file(self, file_id, *, source_id, account_id, entities, result, warnings, is_valid,
                            content_sha256, metrics=None) -> bool:
        self._maybe_raise("complete_file")
        row = self.files.get(int(file_id))
        if row is None or row["status"] != "PROCESSING" or row["state"] != "ACTIVE":
            return False
        summary = await self.replace_file_entities(file_id, entities, source_id=source_id, account_id=account_id)
        return self._update_file(
            file_id,
            [StageChange("persist", "COMPLETED", metrics={**summary, **dict(metrics or {})})],
            {"status": "COMPLETED", "failed_stage": None, "error_message": None, "result_json": dumps_json(dict(result or {})),
             "warnings_json": dumps_json(list(warnings or [])), "entity_count": len(entities),
             "is_valid": None if is_valid is None else int(bool(is_valid)), "content_sha256": content_sha256,
             "processed_at": NOW},
            ("PROCESSING",),
        )

    async def reset_file_for_retry(self, file_id: int) -> bool:
        return self._update_file(
            file_id,
            [StageChange(s, "PENDING") for s in ("download", "extraction", "intelligence", "entities", "persist")],
            {"status": "PENDING", "failed_stage": None, "error_message": None, "attempts": 0},
            ("FAILED",),
            "ACTIVE",
        )

    async def counts_by_source(self, source_id: int | None = None) -> dict[int, dict[str, int]]:
        out: dict[int, dict[str, int]] = {}
        empty = {"files_active": 0, "files_failed": 0, "files_deleted": 0, "entities_active": 0}
        for r in self.files.values():
            if source_id is not None and r["source_id"] != int(source_id):
                continue
            counts = out.setdefault(r["source_id"], dict(empty))
            if r["state"] == "DELETED":
                counts["files_deleted"] += 1
            else:
                counts["files_active"] += 1
                counts["files_failed"] += int(r["status"] == "FAILED")
        for e in self.entities.values():
            if (source_id is None or e["source_id"] == int(source_id)) and e["status"] == "ACTIVE":
                out.setdefault(e["source_id"], dict(empty))["entities_active"] += 1
        return out

    def _update_file(self, file_id, stages, columns, expect_status=None, expect_state=None) -> bool:
        row = self.files.get(int(file_id))
        for name in columns:
            if name not in _FILE_WRITABLE:
                raise ValueError(f"Column not writable: {name!r}")
        if row is None:
            return False
        if expect_status and row["status"] not in expect_status:
            return False
        if expect_state and row["state"] != expect_state:
            return False
        now = utc_now()
        if stages:
            details = loads_json(row.get("stage_details"), {})
            for change in stages:
                details = apply_file_stage_change(details, change, now=now)
                row[file_stage_column(change.stage)] = change.status
            row["stage_details"] = dumps_json(details)
        for name, value in columns.items():
            if value is NOW:
                row[name] = _iso(now)
            elif value is INCREMENT:
                row[name] = int(row.get(name) or 0) + 1
            else:
                row[name] = _stored(value)
        row["updated_at"] = _iso(now)
        return True

    # -- entities --

    async def replace_file_entities(self, file_id, entities: list[EntityRow], *, source_id, account_id, conn=None) -> dict[str, int]:
        now = _iso(utc_now())
        withdrawn = 0
        for entity in self.entities.values():
            if entity["source_file_id"] == int(file_id) and entity["status"] == "ACTIVE":
                entity.update(status="WITHDRAWN", withdrawn_at=now, updated_at=now)
                withdrawn += 1
        for item in entities[:MAX_ENTITIES_PER_FILE]:
            entity_id = self._next("entity")
            self.entities[entity_id] = {
                "id": entity_id, "source_id": int(source_id), "source_file_id": int(file_id), "account_id": account_id,
                "entity_type": item.entity_type, "display_value": item.display_value, "match_key": item.match_key,
                "confidence": item.confidence, "is_valid": None if item.is_valid is None else int(item.is_valid),
                "fields_json": dumps_json(item.fields), "issues_json": dumps_json(list(item.issues)),
                "status": "ACTIVE", "created_at": now, "updated_at": now, "withdrawn_at": None,
            }
        return {"withdrawn": withdrawn, "inserted": min(len(entities), MAX_ENTITIES_PER_FILE)}

    async def withdraw_file_entities(self, file_id: int) -> int:
        return (await self.replace_file_entities(file_id, [], source_id=0, account_id=None))["withdrawn"]

    async def list_entities(self, *, source_id=None, entity_type=None, status=None, q=None, limit=50, offset=0):
        text = " ".join(str(q or "").split()).lower()
        rows = [
            e for e in self.entities.values()
            if (source_id is None or e["source_id"] == int(source_id))
            and (not entity_type or e["entity_type"] == entity_type)
            and (not status or e["status"] == status)
            and (not text or text in (e["display_value"] or "").lower() or text in (e["match_key"] or "").lower())
        ]
        rows.sort(key=lambda e: e["id"], reverse=True)
        return [self._entity_view(e) for e in rows[offset: offset + limit]], len(rows)

    async def get_entity(self, entity_id: int) -> dict[str, Any] | None:
        row = self.entities.get(int(entity_id))
        return self._entity_view(row) if row else None

    # -- monitoring --

    async def store_stats(self) -> dict[str, int]:
        self._maybe_raise("store_stats")
        return {
            "sources": sum(1 for s in self.sources.values() if s["status"] != "DELETED"),
            "entities_active": sum(1 for e in self.entities.values() if e["status"] == "ACTIVE"),
            "files_active": sum(1 for f in self.files.values() if f["state"] == "ACTIVE"),
        }

    async def last_runs(self) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for run_id in sorted(self.runs):
            row = self.runs[run_id]
            if row["status"] in FINISHED_RUN_STATUSES:
                out[row["source_id"]] = copy.deepcopy(row)
        return out

    async def persist_failures(self, run_ids) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        wanted = {int(i) for i in run_ids}
        for f in self.files.values():
            if f["status"] == "FAILED" and f["failed_stage"] == "persist" and f["state"] == "ACTIVE" and f["last_run_id"] in wanted:
                entry = out.setdefault(f["last_run_id"], {"n": 0, "reason": None})
                entry["n"] += 1
                entry["reason"] = max(entry["reason"] or "", f["error_message"] or "") or None
        return out

    async def stuck_runs(self, heartbeat_before: datetime) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "source_id": r["source_id"], "started_at": r["started_at"], "updated_at": r["updated_at"],
             "source_name": (self.sources.get(r["source_id"]) or {}).get("name")}
            for r in self.runs.values()
            if r["status"] == "RUNNING" and parse_utc(r["updated_at"]) < heartbeat_before
        ]

    async def failed_files_since(self, since: datetime) -> list[dict[str, Any]]:
        out = []
        for f in sorted(self.files.values(), key=lambda r: r["id"], reverse=True):
            when = parse_utc(f["processed_at"] or f["updated_at"])
            if f["status"] == "FAILED" and f["state"] == "ACTIVE" and when and when >= since and self._live(f["source_id"]):
                source = self.sources.get(f["source_id"]) or {}
                account = self.accounts.get(source.get("account_id") or -1)
                out.append({**{k: f[k] for k in ("id", "source_id", "name", "path", "failed_stage", "error_message",
                                                  "processed_at", "updated_at")},
                            "source_name": source.get("name"), "account_name": account["name"] if account else None})
        return out

    async def failed_runs_since(self, since: datetime) -> list[dict[str, Any]]:
        out = []
        for r in sorted(self.runs.values(), key=lambda r: r["id"], reverse=True):
            when = parse_utc(r["finished_at"] or r["updated_at"])
            if r["status"] == "FAILED" and when and when >= since and self._live(r["source_id"]):
                source = self.sources.get(r["source_id"]) or {}
                account = self.accounts.get(source.get("account_id") or -1)
                out.append({**{k: r[k] for k in ("id", "source_id", "trigger_type", "error_message", "finished_at", "updated_at")},
                            "source_name": source.get("name"), "account_name": account["name"] if account else None})
        return out

    async def run_activity(self, limit: int) -> list[dict[str, Any]]:
        rows = sorted(self.runs.values(), key=lambda r: (r["updated_at"], r["id"]), reverse=True)[:limit]
        return [{**copy.deepcopy(r), "source_name": (self.sources.get(r["source_id"]) or {}).get("name")} for r in rows]

    async def file_activity(self, limit: int) -> list[dict[str, Any]]:
        rows = sorted(self.files.values(), key=lambda r: (r["updated_at"], r["id"]), reverse=True)[:limit]
        return [{**self._file_view(r), "source_name": (self.sources.get(r["source_id"]) or {}).get("name")} for r in rows]


# ---- the environment ------------------------------------------------------------------------------


@dataclass
class CrmEnv:
    env: Env
    repo: FakeCrmRepo
    box: FakeBox
    graph: FakeGraph
    graph_factory: FakeGraphFactory
    extractor: FakeCrmExtractor
    service: SyncService
    temp_root: Path
    audits: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    wakes: list[int] = field(default_factory=list)
    key_missing: bool = False

    @property
    def settings(self) -> Any:
        return self.env.settings

    def box_factory(self) -> FakeBox:
        if self.key_missing:
            raise SecretsUnavailable(KEY_MISSING_REASON, code="key_missing")
        return self.box

    def source(self, source_id: int) -> dict[str, Any]:
        return self.repo.sources[source_id]

    def run(self, run_id: int) -> dict[str, Any]:
        return self.repo.runs[run_id]

    def file_by_item(self, item_id: str) -> dict[str, Any]:
        return next(f for f in self.repo.files.values() if f["item_id"] == item_id)

    def stages(self, item_id: str) -> dict[str, str]:
        row = self.file_by_item(item_id)
        return {s: row[f"{s}_status"] for s in ("download", "extraction", "intelligence", "entities", "persist")}

    def active_entities(self, item_id: str | None = None) -> list[dict[str, Any]]:
        file_id = self.file_by_item(item_id)["id"] if item_id else None
        return [e for e in self.repo.entities.values() if e["status"] == "ACTIVE" and (file_id is None or e["source_file_id"] == file_id)]

    def seed_source(self, **fields: Any) -> int:
        return self.repo.seed_source(self.box, **fields)

    async def sync(self, source_id: int, *, trigger: str = "MANUAL") -> tuple[int, str]:
        """Queue a run for ``source_id``, claim it and process it; (run id, final status)."""
        run_id = await self.repo.create_run(source_id, trigger_type=trigger, triggered_by=101)
        row = await self.repo.claim_next_run("test-sync-worker")
        assert row is not None and int(row["id"]) == run_id
        return run_id, await self.service.process_run(row)

    def health_deps(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "crm_repo": self.repo,
            "secret_box_factory": self.box_factory,
            "graph_factory": self.graph_factory,
            "sync_worker_running": lambda: True,
        }
        values.update(overrides)
        return self.env.health_deps(**values)

    def runtime(self, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "crm_installed": True,
            "sync_service": self.service,
            "sync_worker": SimpleNamespace(running=True),
            "health": self.health_deps(),
        }
        values.update(overrides)
        return make_runtime(self.env, **values)


@pytest.fixture
def crm_env(env: Env, tmp_path: Path) -> CrmEnv:
    repo = FakeCrmRepo()
    graph = FakeGraph()
    factory = FakeGraphFactory(graph)
    extractor = FakeCrmExtractor()
    temp_root = tmp_path / "crm-tmp"
    temp_root.mkdir()
    holder: dict[str, CrmEnv] = {}

    async def audit(**kw: Any) -> None:
        holder["crm"].audits.append(kw)

    async def error_log(**kw: Any) -> None:
        holder["crm"].errors.append(kw)

    service = SyncService(
        db=None,
        repo=repo,
        settings=env.settings,
        box_factory=lambda: holder["crm"].box_factory(),
        graph_factory=factory,
        extract_fn=extractor,
        site_url_validator=fake_validate_site_url,
        hint_fn=fake_hint,
        audit=audit,
        error_log=error_log,
        temp_root=temp_root,
    )
    crm = CrmEnv(
        env=env, repo=repo, box=FakeBox(), graph=graph, graph_factory=factory, extractor=extractor,
        service=service, temp_root=temp_root,
    )
    holder["crm"] = crm
    service.set_wake_callback(lambda: crm.wakes.append(1))
    return crm


def source_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "Sales contracts",
        "account_id": ACCOUNT_ID,
        "tenant_id": TENANT_ID,
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "site_url": SITE_URL,
        "drive_name": "Documents",
        "folder_path": "/CRM/Contracts",
        "recursive": True,
        "file_extensions": [".pdf", ".docx"],
        "sync_enabled": False,
        "sync_interval_days": 14,
        "sync_hour": 2,
    }
    body.update(overrides)
    return body


def old(minutes: int = 30) -> str:
    return _iso(utc_now() - timedelta(minutes=minutes))
