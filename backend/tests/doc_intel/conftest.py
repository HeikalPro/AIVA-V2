"""Shared fakes for the doc_intel tests.

No test here touches a database, the network, the real storage directory or the real
``.env`` values: settings are built with ``_env_file=None``, the Oracle side is played by
in-memory fakes, HTTP by ``httpx.MockTransport``, and the API is exercised through a
minimal FastAPI app that mounts ONLY the doc-intel router (``backend.main`` is never
imported: its lifespan talks to the configured database).
"""
from __future__ import annotations

import copy
import hashlib
import re
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, Request

from backend.auth.deps import RoleAssignment, UserContext, get_current_user
from backend.auth.role_constants import (
    ROLE_ACCOUNT_MANAGER,
    ROLE_AGENT,
    ROLE_DEVELOPER,
    ROLE_ORG_ADMIN,
    ROLE_SUPER_ADMIN,
    ROLE_SUPERVISOR,
)
from backend.doc_intel import storage as real_storage
from backend.doc_intel.constants import (
    ALLOWED_EXTENSIONS,
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_PUBLISHED,
    DOC_QUEUED,
    DOC_UNPUBLISHED,
    HEALTH_FAILED,
    HEALTH_HEALTHY,
    KB_STAGES,
    MAX_ERROR_BYTES,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
    kb_vertical_for,
)
from backend.doc_intel.extraction import ExtractionFailed
from backend.doc_intel.health import CheckResult, HealthDeps, reset_throttle
from backend.doc_intel.kb_import import KbImportService
from backend.doc_intel.kb_repo import (
    INTERRUPTED_REASON,
    NOW,
    StageChange,
    apply_stage_change,
    dumps_json,
    interrupted_stage,
    loads_json,
    normalize_corpus_id,
    parse_queue_keys,
    parse_utc,
    stage_column,
)
from backend.doc_intel.kb_store import PublishFailed
from backend.doc_intel.normalized import NormalizedBlock, NormalizedDocument, NormalizedPage, NormalizedWarning
from backend.doc_intel.queue_config import (
    UnknownQueueError,
    queues_with_vertical,
    remove_vertical,
    set_vertical_queues,
)
from backend.doc_intel.routers import build_doc_intel_router
from backend.doc_intel.runtime import DocIntelRuntime, get_runtime
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.storage import StoredUpload, UploadRejected
from backend.doc_intel.textutil import truncate_utf8, utc_now
from backend.exceptions import UnauthorizedError
from embedding_service.embedders.result import EmbedBatchResult

# ---- constants -----------------------------------------------------------------------------

CORPUS_ID = "091b8d61c54645ef86df0d78e0b9ae0c"
OTHER_CORPUS_ID = "aa11bb22cc33dd44ee55ff6677889900"
ACCOUNT_ID = 3
EMBED_DIMENSION = 64
EMBED_KEY_ENV = "DI_TEST_EMBED_KEY"
EMBED_BASE_URL = "https://embed.example.test/v1"

ALL_ROLES = (ROLE_SUPER_ADMIN, ROLE_DEVELOPER, ROLE_ORG_ADMIN, ROLE_ACCOUNT_MANAGER, ROLE_SUPERVISOR, ROLE_AGENT)
NON_PLATFORM_ROLES = (ROLE_AGENT, ROLE_SUPERVISOR, ROLE_ACCOUNT_MANAGER, ROLE_ORG_ADMIN)
PDF_BYTES = b"%PDF-1.7\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def corpus_config(**overrides: Any) -> dict[str, Any]:
    """A realistic corpus config_json (includes keys the module must preserve)."""
    cfg: dict[str, Any] = {
        "adapter": "halan_records_v1",
        "chunker_version": "1",
        "chunk_max_chars": 600,
        "chunk_overlap": 80,
        "embedder": {
            "type": "http",
            "model": "text-embedding-3-small",
            "base_url": EMBED_BASE_URL,
            "api_key_env": EMBED_KEY_ENV,
            "dimension": EMBED_DIMENSION,
        },
        "queue_groups": {
            "HALAN": {"label": "Halan", "verticals": ["CF", "Pay"], "ivr_hint": "press 1"},
            "Gomla": {"label": "Gomla", "verticals": ["Gomla"]},
            "Cards": {"label": "Card Support", "verticals": []},
        },
        "custom_setting": {"keep": True},
    }
    cfg.update(overrides)
    return cfg


def make_settings(tmp_path: Path, **overrides: Any) -> DocIntelSettings:
    values: dict[str, Any] = {
        "storage_dir": str(tmp_path / "storage"),
        "audit_enabled": False,
        "error_log_enabled": False,
        "worker_poll_seconds": 0.5,
        "health_min_interval_seconds": 0,
        "embed_max_retries": 2,
    }
    values.update(overrides)
    return DocIntelSettings(_env_file=None, **values)


def make_user(role: str, user_id: int | None = None) -> UserContext:
    index = ALL_ROLES.index(role) + 1 if role in ALL_ROLES else 99
    return UserContext(
        id=user_id or 100 + index,
        email=f"{role.lower()}@example.test",
        organization_id=1,
        first_name="Test",
        last_name=role.title(),
        status="ACTIVE",
        roles=[RoleAssignment(role_id=index, role_name=role)],
    )


def make_document(filename: str = "Card FAQ.pdf", *, pages: int = 2, paragraphs_per_page: int = 3) -> NormalizedDocument:
    blocks: list[NormalizedBlock] = [NormalizedBlock(kind="heading", text="Cards", pages=[1], heading_path=[], level=1)]
    page_texts: list[str] = []
    for p in range(1, pages + 1):
        texts = []
        for i in range(paragraphs_per_page):
            text = (
                f"Page {p} paragraph {i}: customers can request a replacement card from the app. "
                "The fee is deducted from the wallet balance. Delivery takes three working days."
            )
            blocks.append(NormalizedBlock(kind="paragraph", text=text, pages=[p], heading_path=["Cards"]))
            texts.append(text)
        page_texts.append("\n\n".join(texts))
    return NormalizedDocument(
        filename=filename,
        media_type="application/pdf",
        sha256="0" * 64,
        page_count=pages,
        blocks=blocks,
        pages=[NormalizedPage(number=i + 1, text=t, classification="text") for i, t in enumerate(page_texts)],
        warnings=[NormalizedWarning(code="low_text", message="Page 2 has little text", page=2)],
        extractor={"name": "document-extractor", "version": "0.1.0", "mode": "balanced", "pdf_engine": "pdfium"},
    )


# ---- in-memory registry (stands in for KbRepo) ---------------------------------------------


def _iso(value: datetime) -> str:
    # What backend.database returns for a TIMESTAMP column: naive isoformat().
    return value.isoformat()


class FakeRepo:
    """In-memory ``KbRepo`` with the same row shape and state rules (no SQL)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.accounts: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self.recover_calls: list[datetime] = []
        self.touches: list[int] = []
        self.claim_error: Exception | None = None
        self.refuse_updates = False  # simulate "no longer PROCESSING"

    # -- setup helpers --

    def add_account(self, account_id: int = ACCOUNT_ID, corpus_id: str | None = CORPUS_ID, name: str = "Hallan") -> None:
        self.accounts[account_id] = {"id": account_id, "name": name, "corpus_id": corpus_id}

    def seed(self, **fields: Any) -> int:
        """Insert a row directly (for state-machine tests)."""
        doc_id = self._next_id
        self._next_id += 1
        now = _iso(utc_now())
        row: dict[str, Any] = {
            "id": doc_id,
            "batch_id": "b" * 32,
            "account_id": ACCOUNT_ID,
            "corpus_id": CORPUS_ID,
            "queue_keys": dumps_json(["HALAN"]),
            "vertical": kb_vertical_for(doc_id),
            "filename": "seeded.pdf",
            "content_type": ALLOWED_EXTENSIONS[".pdf"],
            "size_bytes": 100,
            "sha256": uuid.uuid4().hex * 2,
            "storage_dir": None,
            "status": DOC_QUEUED,
            "failed_stage": None,
            "error_message": None,
            "stage_details": "{}",
            "warnings_json": None,
            "page_count": None,
            "chunk_count": None,
            "tokens_used": None,
            "cost_usd": None,
            "attempts": 0,
            "worker_id": None,
            "uploaded_by": 101,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "published_at": None,
        }
        for stage in KB_STAGES:
            row[stage_column(stage)] = STAGE_PENDING
        row["upload_status"] = STAGE_COMPLETED
        if "queue_keys" in fields and not isinstance(fields["queue_keys"], str):
            fields["queue_keys"] = dumps_json(fields["queue_keys"])
        row.update(fields)
        self.rows[doc_id] = row
        return doc_id

    def _joined(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        account = self.accounts.get(int(row["account_id"]))
        out["account_name"] = account["name"] if account else None
        out["organization_name"] = "GoChat247" if account else None
        out["uploaded_by_email"] = "super_admin@example.test" if row.get("uploaded_by") else None
        return out

    # -- accounts --

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        account = self.accounts.get(int(account_id))
        return dict(account) if account else None

    async def distinct_account_corpora(self) -> list[str]:
        out: list[str] = []
        for account in self.accounts.values():
            cid = normalize_corpus_id(account.get("corpus_id"))
            if cid and cid not in out:
                out.append(cid)
        return out

    # -- documents --

    async def insert_document(self, **kw: Any) -> int:
        statuses = kw.pop("stage_statuses")
        finished = kw.pop("finished", False)
        details = kw.pop("stage_details", None) or {}
        queue_keys = kw.pop("queue_keys")
        now = utc_now()
        fields = {
            **kw,
            "queue_keys": dumps_json(list(queue_keys)),
            "stage_details": dumps_json(details),
            "error_message": truncate_utf8(kw.get("error_message"), MAX_ERROR_BYTES),
            "finished_at": _iso(now) if finished else None,
        }
        for stage in KB_STAGES:
            fields[stage_column(stage)] = statuses.get(stage, STAGE_PENDING)
        return self.seed(**fields)

    async def get_document(self, doc_id: int) -> dict[str, Any] | None:
        row = self.rows.get(int(doc_id))
        return self._joined(row) if row else None

    async def list_documents(self, *, account_id=None, status=None, batch_id=None, limit=50, offset=0):
        rows = [
            r
            for r in self.rows.values()
            if (account_id is None or r["account_id"] == account_id)
            and (not status or r["status"] == status)
            and (not batch_id or r["batch_id"] == batch_id)
        ]
        rows.sort(key=lambda r: r["id"], reverse=True)
        return [self._joined(r) for r in rows[offset : offset + limit]], len(rows)

    async def queue_positions(self) -> dict[int, int]:
        queued = sorted(i for i, r in self.rows.items() if r["status"] == DOC_QUEUED)
        return {doc_id: pos + 1 for pos, doc_id in enumerate(queued)}

    async def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        if self.claim_error is not None:
            err, self.claim_error = self.claim_error, None
            raise err
        for doc_id in sorted(self.rows):
            row = self.rows[doc_id]
            if row["status"] == DOC_QUEUED:
                now = _iso(utc_now())
                row.update(
                    status=DOC_PROCESSING,
                    attempts=int(row["attempts"]) + 1,
                    worker_id=worker_id[:128],
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
                return self._joined(row)
        return None

    async def set_stage(self, doc_id, stage, status, *, error=None, metrics=None, started_at=None) -> bool:
        columns: dict[str, Any] = {}
        if status == STAGE_FAILED:
            columns["failed_stage"] = stage
            columns["error_message"] = truncate_utf8(error or "Failed", MAX_ERROR_BYTES)
        return self._update(
            doc_id,
            [StageChange(stage, status, error=error, metrics=metrics, started_at=started_at)],
            columns,
            (DOC_PROCESSING,),
        )

    async def set_results(self, doc_id, **fields: Any) -> bool:
        columns = {k: v for k, v in fields.items() if k != "warnings"}
        if "warnings" in fields:
            columns["warnings_json"] = dumps_json(list(fields["warnings"] or []))
        return self._update(doc_id, [], columns, (DOC_PROCESSING,))

    async def finish(self, doc_id, *, status, stage=None, error=None, metrics=None, page_count=None,
                     chunk_count=None, tokens_used=None, cost_usd=None) -> bool:
        columns: dict[str, Any] = {"status": status, "finished_at": NOW}
        if status == DOC_PUBLISHED:
            stages = [StageChange(stage or "publishing", STAGE_COMPLETED, metrics=metrics)]
            columns.update(published_at=NOW, failed_stage=None, error_message=None)
            for name, value in (("page_count", page_count), ("chunk_count", chunk_count),
                                ("tokens_used", tokens_used), ("cost_usd", cost_usd)):
                if value is not None:
                    columns[name] = value
        else:
            reason = truncate_utf8(error or "Failed", MAX_ERROR_BYTES)
            stages = [StageChange(stage or "publishing", STAGE_FAILED, error=reason, metrics=metrics)]
            columns.update(failed_stage=stage or "publishing", error_message=reason)
        return self._update(doc_id, stages, columns, (DOC_PROCESSING,))

    async def touch(self, doc_id: int, worker_id: str | None = None) -> None:
        self.touches.append(int(doc_id))
        row = self.rows.get(int(doc_id))
        if row and row["status"] == DOC_PROCESSING and (worker_id is None or row["worker_id"] == worker_id):
            row["updated_at"] = _iso(utc_now())

    async def recover_stale(self, older_than: datetime) -> list[int]:
        self.recover_calls.append(older_than)
        recovered = []
        for doc_id, row in list(self.rows.items()):
            touched = parse_utc(row["updated_at"])
            if row["status"] == DOC_PROCESSING and touched is not None and touched < older_than:
                stage = interrupted_stage(row)
                self._update(
                    doc_id,
                    [StageChange(stage, STAGE_FAILED, error=INTERRUPTED_REASON)],
                    {"status": DOC_FAILED, "failed_stage": stage, "error_message": INTERRUPTED_REASON, "finished_at": NOW},
                    (DOC_PROCESSING,),
                )
                recovered.append(doc_id)
        return recovered

    async def release_interrupted(self, doc_id: int, worker_id: str) -> bool:
        row = self.rows.get(int(doc_id))
        if not row or row["status"] != DOC_PROCESSING or row["worker_id"] != worker_id:
            return False
        stage = interrupted_stage(row)
        return self._update(
            doc_id,
            [StageChange(stage, STAGE_FAILED, error=INTERRUPTED_REASON)],
            {"status": DOC_FAILED, "failed_stage": stage, "error_message": INTERRUPTED_REASON, "finished_at": NOW},
            (DOC_PROCESSING,),
        )

    async def find_active_duplicate(self, corpus_id: str, sha256: str) -> int | None:
        for doc_id in sorted(self.rows):
            row = self.rows[doc_id]
            if row["corpus_id"] == corpus_id and row["sha256"] == sha256 and row["status"] in (DOC_QUEUED, DOC_PROCESSING, DOC_PUBLISHED):
                return doc_id
        return None

    async def reset_for_retry(self, doc_id: int, from_stage: str) -> bool:
        start = KB_STAGES.index(from_stage)
        return self._update(
            doc_id,
            [StageChange(s, STAGE_PENDING) for s in KB_STAGES[start:]],
            {"status": DOC_QUEUED, "failed_stage": None, "error_message": None, "finished_at": None},
            (DOC_FAILED,),
        )

    async def reset_for_republish(self, doc_id: int) -> bool:
        return self._update(
            doc_id,
            [StageChange(s, STAGE_PENDING) for s in ("chunking", "embedding", "publishing")],
            {"status": DOC_QUEUED, "failed_stage": None, "error_message": None, "finished_at": None},
            (DOC_PUBLISHED, DOC_FAILED),
        )

    async def mark_unpublished(self, doc_id: int) -> bool:
        return self._update(doc_id, [], {"status": DOC_UNPUBLISHED}, (DOC_PUBLISHED, DOC_FAILED))

    async def update_queue_keys(self, doc_id: int, queue_keys, *, statuses) -> bool:
        return self._update(doc_id, [], {"queue_keys": dumps_json(list(queue_keys))}, tuple(statuses))

    # -- monitoring --

    async def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return counts

    async def stuck_documents(self, started_before: datetime) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "filename": r["filename"], "started_at": r["started_at"], "updated_at": r["updated_at"]}
            for r in self.rows.values()
            if r["status"] == DOC_PROCESSING and r["started_at"] and parse_utc(r["started_at"]) < started_before
        ]

    async def failed_since(self, since: datetime):
        rows = [
            {k: r[k] for k in ("id", "filename", "failed_stage", "error_message", "finished_at")}
            for r in self.rows.values()
            if r["status"] == DOC_FAILED and (parse_utc(r["finished_at"] or r["updated_at"]) or since) >= since
        ]
        return len(rows), rows[:20]

    async def published_documents(self) -> list[dict[str, Any]]:
        return [
            {k: r[k] for k in ("id", "filename", "corpus_id", "vertical", "queue_keys", "chunk_count")}
            for r in sorted(self.rows.values(), key=lambda r: r["id"])
            if r["status"] == DOC_PUBLISHED
        ]

    async def failures(self, days: int) -> list[dict[str, Any]]:
        since = utc_now() - timedelta(days=days)
        out = []
        for r in sorted(self.rows.values(), key=lambda r: r["id"], reverse=True):
            when = parse_utc(r["finished_at"] or r["updated_at"])
            if r["status"] == DOC_FAILED and when and when >= since:
                joined = self._joined(r)
                out.append({k: joined[k] for k in ("id", "filename", "failed_stage", "error_message", "finished_at", "updated_at", "account_name")})
        return out

    async def activity(self, limit: int) -> list[dict[str, Any]]:
        rows = sorted(self.rows.values(), key=lambda r: (r["updated_at"], r["id"]), reverse=True)[:limit]
        return [self._joined(r) for r in rows]

    # -- shared update rule (mirrors KbRepo._update) --

    def _update(self, doc_id, stages, columns, expect_status) -> bool:
        row = self.rows.get(int(doc_id))
        if row is None or self.refuse_updates:
            return False
        if expect_status and row["status"] not in expect_status:
            return False
        now = utc_now()
        if stages:
            details = loads_json(row.get("stage_details"), {})
            for change in stages:
                details = apply_stage_change(details, change, now=now)
                row[stage_column(change.stage)] = change.status
            row["stage_details"] = dumps_json(details)
        for name, value in columns.items():
            row[name] = _iso(now) if value is NOW else value
        row["updated_at"] = _iso(now)
        return True


# ---- fake knowledge base (stands in for KbStore) -------------------------------------------


class FakeKbStore:
    def __init__(self) -> None:
        self.configs: dict[str, dict[str, Any]] = {}
        self.chunks: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.publish_error: Exception | None = None
        self.config_error: Exception | None = None
        self.ping_error: Exception | None = None
        self.calls: list[str] = []
        self.connection_factory = None

    def add_corpus(self, corpus_id: str = CORPUS_ID, config: dict[str, Any] | None = None) -> None:
        self.configs[corpus_id] = copy.deepcopy(config if config is not None else corpus_config())

    def get_corpus_config(self, corpus_id_hex: str) -> dict[str, Any] | None:
        self.calls.append("get_corpus_config")
        if self.config_error is not None:
            raise self.config_error
        cfg = self.configs.get(corpus_id_hex)
        return copy.deepcopy(cfg) if cfg is not None else None

    def publish(self, *, corpus_id_hex, vertical, queue_keys, chunks, vectors, embedding_model, dimension) -> dict[str, Any]:
        self.calls.append("publish")
        if self.publish_error is not None:
            raise self.publish_error
        if corpus_id_hex not in self.configs:
            raise PublishFailed("Knowledge base corpus not found", code="corpus_not_found")
        assert len(chunks) == len(vectors)
        assert all(len(v) == dimension for v in vectors)
        try:
            after = set_vertical_queues(self.configs[corpus_id_hex], vertical, queue_keys)
        except UnknownQueueError as ex:
            raise PublishFailed(f"Queue '{ex.key}' no longer exists in this knowledge base", code="unknown_queue") from None
        self.chunks[(corpus_id_hex, vertical)] = [
            {"index": c.index, "text": c.text, "payload": c.payload, "vector": v, "model": embedding_model}
            for c, v in zip(chunks, vectors)
        ]
        self.configs[corpus_id_hex] = after
        return {"chunks_written": len(chunks), "chunks_replaced": 0, "queues": {k: {} for k in queue_keys}}

    def unpublish(self, *, corpus_id_hex: str, vertical: str) -> dict[str, Any]:
        self.calls.append("unpublish")
        if self.publish_error is not None:
            raise self.publish_error
        before = self.configs.get(corpus_id_hex)
        removed = queues_with_vertical(before, vertical) if before is not None else []
        if before is not None:
            self.configs[corpus_id_hex] = remove_vertical(before, vertical)
        deleted = len(self.chunks.pop((corpus_id_hex, vertical), []))
        return {"corpus_found": before is not None, "removed_from_queues": removed, "chunks_deleted": deleted}

    def set_queues(self, *, corpus_id_hex: str, vertical: str, queue_keys: list[str]) -> dict[str, Any]:
        self.calls.append("set_queues")
        if self.publish_error is not None:
            raise self.publish_error
        if corpus_id_hex not in self.configs:
            raise PublishFailed("Knowledge base corpus not found", code="corpus_not_found")
        self.configs[corpus_id_hex] = set_vertical_queues(self.configs[corpus_id_hex], vertical, queue_keys)
        return {"queues": {}}

    def chunk_counts(self, corpus_id_hex: str, parent_ids) -> dict[str, int]:
        return {pid: len(self.chunks.get((corpus_id_hex, pid), [])) for pid in parent_ids}

    def ping(self) -> int:
        if self.ping_error is not None:
            raise self.ping_error
        return 3

    def queues_of(self, vertical: str, corpus_id: str = CORPUS_ID) -> list[str]:
        return queues_with_vertical(self.configs.get(corpus_id), vertical)


# ---- embedder / extractor / storage fakes --------------------------------------------------


class FakeEmbedder:
    """HTTP-style embedder: accepts ``conn=None``; optional scripted errors per call."""

    def __init__(self, dimension: int = EMBED_DIMENSION, *, errors: list[Exception | None] | None = None,
                 report_tokens: bool = True) -> None:
        self.dimension = dimension
        self.errors = list(errors or [])
        self.report_tokens = report_tokens
        self.calls: list[tuple[int, Any]] = []

    def embed(self, texts: list[str], conn: Any = None) -> EmbedBatchResult:
        self.calls.append((len(texts), conn))
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        vectors = [[((len(t) + i) % 11) / 10.0 for i in range(self.dimension)] for t in texts]
        return EmbedBatchResult(
            vectors=vectors,
            api_total_tokens=sum(len(t) // 4 for t in texts) if self.report_tokens else None,
            estimated_tokens=sum(max(1, len(t) // 4) for t in texts),
        )


class FakeExtractor:
    """Stands in for ``extraction.run_extraction``: writes normalized.json and returns the document."""

    def __init__(self, document: NormalizedDocument | None = None, *, error: Exception | None = None) -> None:
        self.document = document
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def __call__(self, source_path: Path, *, filename: str, media_type: str, sha256: str, out_path: Path,
                 settings: DocIntelSettings) -> NormalizedDocument:
        self.calls.append({"source": source_path, "filename": filename, "media_type": media_type, "out_path": out_path})
        if self.error is not None:
            raise self.error
        doc = (self.document or make_document(filename)).model_copy(update={"filename": filename, "sha256": sha256})
        doc.save(out_path)
        return doc


class FakeStorage:
    """Stands in for ``backend.doc_intel.storage`` (Agent 4's module) under a temp dir."""

    UploadRejected = UploadRejected

    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[Path] = []

    def sanitize_filename(self, name: str | None) -> str:
        base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
        return base[:255] or "upload"

    def new_document_dir(self, settings: DocIntelSettings) -> Path:
        path = self.root / "kb" / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path

    async def save_upload(self, upload: Any, doc_dir: Path, *, settings: DocIntelSettings) -> StoredUpload:
        data = await upload.read()
        name = self.sanitize_filename(upload.filename)
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise UploadRejected("Unsupported file type (only PDF and DOCX)", code="unsupported_type")
        if ext == ".pdf" and not data.startswith(b"%PDF-"):
            raise UploadRejected("The file is not a valid PDF", code="bad_magic")
        if len(data) > settings.max_upload_bytes:
            raise UploadRejected(f"File is too large; limit is {settings.max_upload_mb} MB", code="too_large")
        original = doc_dir / f"original{ext}"
        original.write_bytes(data)
        return StoredUpload(
            filename=name,
            kind="pdf" if ext == ".pdf" else "docx",
            content_type=ALLOWED_EXTENSIONS[ext],
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            doc_dir=doc_dir,
            original_path=original,
        )

    def normalized_path(self, doc_dir: Path) -> Path:
        return real_storage.normalized_path(doc_dir)

    def remove_document_dir(self, doc_dir: Path, *, settings: DocIntelSettings) -> None:
        self.removed.append(doc_dir)
        shutil.rmtree(doc_dir, ignore_errors=True)


class FakeUpload:
    """Minimal UploadFile stand-in for service-level tests (a stream: read() ends with b"")."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.size = len(data)
        self._data = data
        self._pos = 0

    async def read(self, size: int = -1) -> bytes:
        end = len(self._data) if size is None or size < 0 else self._pos + size
        chunk = self._data[self._pos : end]
        self._pos += len(chunk)
        return chunk


class FakeExtractionModule:
    """Stands in for the extraction module in the health / status paths."""

    def __init__(self, *, available: bool = True, reason: str | None = None, smoke: dict[str, Any] | None = None) -> None:
        self.available = available
        self.reason = reason
        self.smoke = smoke or {
            "ok": True,
            "seconds": 0.4,
            "detail": None,
            "info": {
                "document_extractor_version": "0.1.0",
                "tesseract_available": True,
                "ocr_languages_requested": ["ara", "eng"],
                "ocr_languages_installed": ["ara", "eng", "osd"],
            },
        }
        self.smoke_calls = 0

    def extraction_available(self) -> tuple[bool, str | None]:
        return self.available, self.reason

    def run_smoke_test(self, settings: DocIntelSettings, *, timeout_seconds: float) -> dict[str, Any]:
        self.smoke_calls += 1
        return copy.deepcopy(self.smoke)


class FakeAppDb:
    """The app-pool ``Database`` as used by the database health check."""

    def __init__(self) -> None:
        self.error: Exception | None = None

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any] | None:
        if self.error is not None:
            raise self.error
        return {"ok": 1}


class FakeHealthRepo:
    """In-memory ``HealthRepo`` with the same MERGE / transition rules."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.save_error: Exception | None = None
        self.load_error: Exception | None = None

    async def load_all(self) -> list[dict[str, Any]]:
        if self.load_error is not None:
            raise self.load_error
        return [dict(r) for r in self.rows.values()]

    async def save(self, result: CheckResult, *, label: str, checked_at: datetime) -> bool:
        if self.save_error is not None:
            raise self.save_error
        prev = self.rows.get(result.key)
        stamp = _iso(checked_at)
        row = {
            "component_key": result.key,
            "label": label,
            "status": result.status,
            "reason": result.reason,
            "suggested_action": result.suggested_action,
            "details_json": dumps_json(result.details or {}),
            "latency_ms": result.latency_ms,
            "checked_at": stamp,
            "last_success_at": stamp if result.status == HEALTH_HEALTHY else (prev or {}).get("last_success_at"),
            "last_failure_at": stamp if result.status == HEALTH_FAILED else (prev or {}).get("last_failure_at"),
            "consecutive_failures": ((prev or {}).get("consecutive_failures", 0) + 1) if result.status == HEALTH_FAILED else 0,
        }
        self.rows[result.key] = row
        old = prev["status"] if prev else None
        if old == result.status:
            return False
        self.events.append(
            {
                "id": len(self.events) + 1,
                "component_key": result.key,
                "old_status": old,
                "new_status": result.status,
                "reason": result.reason,
                "created_at": stamp,
            }
        )
        return True

    async def list_events(self, limit: int) -> list[dict[str, Any]]:
        return list(reversed(self.events))[:limit]


# ---- SQL-recording fakes (exercise the real KbRepo / HealthRepo / KbStore SQL) ------------

_QUOTED = re.compile(r"'(?:[^']|'')*'")
_BIND = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")


def bind_names(sql: str) -> set[str]:
    """Named placeholders in ``sql`` (string literals ignored)."""
    return set(_BIND.findall(_QUOTED.sub("''", sql)))


def check_binds(sql: str, params: dict[str, Any] | None, *, extra: set[str] = frozenset()) -> None:
    """Oracle rejects both missing and unused named binds: the sets must match exactly."""
    expected = bind_names(sql)
    given = set(params or {}) | set(extra)
    assert expected == given, f"bind mismatch: SQL uses {sorted(expected)}, given {sorted(given)}\n{sql}"


class _Responder:
    def __init__(self) -> None:
        self.rules: list[tuple[re.Pattern[str], Any]] = []

    def on(self, pattern: str, result: Any) -> None:
        """``result``: a value, or a callable(sql, params) -> value. Later rules win."""
        self.rules.insert(0, (re.compile(pattern, re.IGNORECASE | re.DOTALL), result))

    def answer(self, sql: str, params: dict[str, Any] | None, default: Any) -> Any:
        for pattern, result in self.rules:
            if pattern.search(sql):
                return result(sql, params) if callable(result) else copy.deepcopy(result)
        return default


class RecordingDatabase(_Responder):
    """Stands in for ``backend.database.Database``: records statements, validates binds."""

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.rowcounts: list[int] = []  # consumed by cursor.execute; default 1
        self.commits = 0
        self.rollbacks = 0
        self._next_id = 41

    def _record(self, sql: str, params: dict[str, Any] | None, *, extra: set[str] = frozenset()) -> None:
        check_binds(sql, params, extra=extra)
        self.statements.append((" ".join(sql.split()), dict(params or {})))

    def sql_matching(self, pattern: str) -> list[tuple[str, dict[str, Any]]]:
        rx = re.compile(pattern, re.IGNORECASE)
        return [(s, p) for s, p in self.statements if rx.search(s)]

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Any]:
        try:
            yield _RecordingAsyncConnection(self)
            self.commits += 1
        except Exception:
            self.rollbacks += 1
            raise

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None) -> Any:
        self._record(sql, params)
        return self.answer(sql, params, None)

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None) -> Any:
        self._record(sql, params)
        return self.answer(sql, params, [])

    async def execute(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None, return_id: bool = False) -> Any:
        self._record(sql, params, extra={"out_id"} if return_id else set())
        if return_id:
            self._next_id += 1
            return self._next_id
        return None


class _RecordingAsyncCursor:
    def __init__(self, db: RecordingDatabase) -> None:
        self._db = db
        self.rowcount = 0

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._db._record(sql, params)
        self.rowcount = self._db.rowcounts.pop(0) if self._db.rowcounts else 1


class _RecordingAsyncConnection:
    def __init__(self, db: RecordingDatabase) -> None:
        self._db = db

    def cursor(self) -> _RecordingAsyncCursor:
        return _RecordingAsyncCursor(self._db)


class RecordingKbConnection(_Responder):
    """Sync KB-pool connection factory (``EmbeddingService.db.connection`` stand-in)."""

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[tuple[str, Any]] = []
        self.transactions: list[str] = []  # "commit" / "rollback" per connection() use
        self.rowcounts: dict[str, int] = {}  # regex -> rowcount for matching execute()
        self.fail_on: tuple[re.Pattern[str], Exception] | None = None

    def raise_on(self, pattern: str, error: Exception) -> None:
        self.fail_on = (re.compile(pattern, re.IGNORECASE | re.DOTALL), error)

    def verbs(self) -> list[str]:
        return [s.split()[0].upper() + (" FOR UPDATE" if "FOR UPDATE" in s.upper() else "") for s, _ in self.statements]

    @contextmanager
    def __call__(self) -> Iterator[Any]:
        try:
            yield _RecordingKbConn(self)
            self.transactions.append("commit")
        except Exception:
            self.transactions.append("rollback")
            raise


class _RecordingKbCursor:
    def __init__(self, owner: RecordingKbConnection) -> None:
        self._owner = owner
        self.rowcount = 0
        self._result: Any = None

    def __enter__(self) -> _RecordingKbCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def _maybe_fail(self, sql: str) -> None:
        if self._owner.fail_on and self._owner.fail_on[0].search(sql):
            raise self._owner.fail_on[1]

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        check_binds(sql, params)
        self._maybe_fail(sql)
        self._owner.statements.append((" ".join(sql.split()), dict(params or {})))
        self._result = self._owner.answer(sql, params, None)
        self.rowcount = 1
        for pattern, count in self._owner.rowcounts.items():
            if re.search(pattern, sql, re.IGNORECASE):
                self.rowcount = count

    def executemany(self, sql: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            check_binds(sql, row)
        self._maybe_fail(sql)
        self._owner.statements.append((" ".join(sql.split()), [dict(r) for r in rows]))
        self.rowcount = len(rows)

    def fetchone(self) -> Any:
        result = self._result
        if isinstance(result, list):
            return result[0] if result else None
        return result

    def fetchall(self) -> list[Any]:
        result = self._result
        if result is None:
            return []
        return result if isinstance(result, list) else [result]


class _RecordingKbConn:
    def __init__(self, owner: RecordingKbConnection) -> None:
        self._owner = owner

    def cursor(self) -> _RecordingKbCursor:
        return _RecordingKbCursor(self._owner)


# ---- service / API harness -----------------------------------------------------------------


@dataclass
class Env:
    settings: DocIntelSettings
    repo: FakeRepo
    kb: FakeKbStore
    storage: FakeStorage
    extractor: FakeExtractor
    embedder: FakeEmbedder
    service: KbImportService
    health_repo: FakeHealthRepo
    app_db: FakeAppDb
    extraction: FakeExtractionModule
    audits: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    wakes: list[int] = field(default_factory=list)
    http_handler: Callable[[httpx.Request], httpx.Response] | None = None

    def health_deps(self, **overrides: Any) -> HealthDeps:
        values: dict[str, Any] = {
            "db": self.app_db,
            "kb": self.kb,
            "kb_repo": self.repo,
            "health_repo": self.health_repo,
            "settings": self.settings,
            "embedding_settings": SimpleNamespace(default_openai_api_key=None),
            "worker_running": lambda: True,
            "extraction": self.extraction,
            "http_client_factory": lambda: httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
            "app_db_service": "FREEPDB1",
            "kb_db_service": "FREEPDB1",
        }
        values.update(overrides)
        return HealthDeps(**values)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.http_handler is not None:
            return self.http_handler(request)
        return httpx.Response(200, json={"data": []})

    async def upload(self, *files: tuple[str, bytes], queue_keys: list[str] | None = None, role: str = ROLE_SUPER_ADMIN):
        return await self.service.upload(
            make_user(role), ACCOUNT_ID, queue_keys or ["HALAN"], [FakeUpload(name, data) for name, data in files]
        )

    async def process_next(self) -> str | None:
        row = await self.repo.claim_next("test-worker")
        if row is None:
            return None
        return await self.service.process_document(row)

    def row(self, doc_id: int) -> dict[str, Any]:
        return self.repo.rows[doc_id]

    def stage_statuses(self, doc_id: int) -> dict[str, str]:
        return {stage: self.repo.rows[doc_id][stage_column(stage)] for stage in KB_STAGES}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    reset_throttle()
    monkeypatch.setenv(EMBED_KEY_ENV, "sk-test-embedding-key-123456")
    settings = make_settings(tmp_path)
    repo = FakeRepo()
    repo.add_account()
    kb = FakeKbStore()
    kb.add_corpus()
    storage = FakeStorage(tmp_path / "storage")
    extractor = FakeExtractor()
    holder: dict[str, Any] = {}

    async def audit(**kw: Any) -> None:
        holder["env"].audits.append(kw)

    async def error_log(**kw: Any) -> None:
        holder["env"].errors.append(kw)

    service = KbImportService(
        db=None,
        repo=repo,
        kb=kb,
        settings=settings,
        embedder_factory=lambda cfg: holder["env"].embedder,
        extract_fn=extractor,
        storage=storage,
        audit=audit,
        error_log=error_log,
        default_price_per_million=0.02,
    )
    environment = Env(
        settings=settings,
        repo=repo,
        kb=kb,
        storage=storage,
        extractor=extractor,
        embedder=FakeEmbedder(),
        service=service,
        health_repo=FakeHealthRepo(),
        app_db=FakeAppDb(),
        extraction=FakeExtractionModule(),
    )
    holder["env"] = environment
    service.set_wake_callback(lambda: environment.wakes.append(1))
    yield environment
    reset_throttle()


async def fake_current_user(request: Request) -> UserContext:
    """Identity from the ``X-Test-Role`` header; none = anonymous (401, like a missing token)."""
    role = request.headers.get("x-test-role")
    if not role:
        raise UnauthorizedError("Missing bearer token")
    return make_user(role)


def build_test_app(runtime: DocIntelRuntime | None, settings: DocIntelSettings) -> FastAPI:
    """A FastAPI app with ONLY the doc-intel router, auth + runtime dependencies overridden."""
    app = FastAPI()
    router = build_doc_intel_router(settings)
    assert router is not None
    api = APIRouter(prefix="/api")
    api.include_router(router)
    app.include_router(api)
    app.dependency_overrides[get_current_user] = fake_current_user
    app.dependency_overrides[get_runtime] = lambda: runtime
    return app


def make_runtime(env: Env, **overrides: Any) -> DocIntelRuntime:
    values: dict[str, Any] = {
        "settings": env.settings,
        "installed": True,
        "schema_version": "001",
        "service": env.service,
        "worker": SimpleNamespace(running=True),
        "kb": env.kb,
        "health": env.health_deps(),
        "extraction": env.extraction,
    }
    values.update(overrides)
    return DocIntelRuntime(**values)


@dataclass
class Api:
    client: httpx.AsyncClient
    app: FastAPI
    runtime: DocIntelRuntime | None
    env: Env

    async def call(self, method: str, path: str, role: str | None = ROLE_SUPER_ADMIN, **kw: Any) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        if role:
            headers["X-Test-Role"] = role
        return await self.client.request(method, path, headers=headers, **kw)


@asynccontextmanager
async def api_client(env: Env, runtime: DocIntelRuntime | None) -> AsyncIterator[Api]:
    app = build_test_app(runtime, env.settings)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Api(client=client, app=app, runtime=runtime, env=env)


@pytest_asyncio.fixture
async def api(env: Env) -> AsyncIterator[Api]:
    async with api_client(env, make_runtime(env)) as harness:
        yield harness


def doc_intel_routes(app: FastAPI) -> list[tuple[str, str, Callable[..., Any]]]:
    """(method, path, endpoint) of every mounted /api/doc-intel route.

    Works with FastAPI's lazily included routers (0.141+, ``iter_route_contexts``) and
    with older versions that copy ``APIRoute`` objects into the parent router.
    """
    from fastapi.routing import APIRoute

    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # older FastAPI
        iter_route_contexts = None
    found: list[tuple[str, str, Callable[..., Any]]] = []
    if iter_route_contexts is not None:
        candidates = [(c.path, c.methods, c.endpoint) for c in iter_route_contexts(app.routes)]
    else:
        candidates = [(r.path, r.methods, r.endpoint) for r in app.routes if isinstance(r, APIRoute)]
    for path, methods, endpoint in candidates:
        if not path or not path.startswith("/api/doc-intel"):
            continue
        for method in sorted(methods or []):
            if method != "HEAD":
                found.append((method, path, endpoint))
    return found
