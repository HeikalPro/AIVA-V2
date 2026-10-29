"""Flow 1: manual knowledge-document import.

upload (HTTP request) -> extraction -> chunking -> embedding -> publishing (background worker)

``AIVA_kb_documents`` rows are the work queue: the upload inserts QUEUED rows, the
worker claims one at a time with a conditional UPDATE, runs the remaining stages and
records each stage (RUNNING -> COMPLETED / FAILED + reason) on the row. Heavy work never
runs on the event loop: extraction (child process), chunking, embedding (HTTP, no DB
connection held) and publishing (one KB transaction) all go through ``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import traceback
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from backend.auth.deps import UserContext
from backend.config import get_settings as get_backend_settings
from backend.doc_intel import extraction as extraction_module
from backend.doc_intel import storage as storage_module
from backend.doc_intel.chunking import ChunkingFailed, build_chunks
from backend.doc_intel.constants import (
    ALLOWED_EXTENSIONS,
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_PUBLISHED,
    DOC_QUEUED,
    DOC_UNPUBLISHED,
    KB_STAGES,
    MAX_ERROR_BYTES,
    MAX_FILENAME_CHARS,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    kb_vertical_for,
)
from backend.doc_intel.embedding import EmbeddingFailed, embed_texts, scrub_secrets
from backend.doc_intel.extraction import ExtractionFailed
from backend.doc_intel.kb_repo import (
    StageChange,
    apply_stage_change,
    normalize_corpus_id,
    parse_queue_keys,
    row_to_out,
)
from backend.doc_intel.kb_store import PublishFailed
from backend.doc_intel.normalized import NormalizedDocument, NormalizedPage
from backend.doc_intel.queue_config import queue_labels
from backend.doc_intel.runtime import ServiceUnavailableError
from backend.doc_intel.schemas import (
    KbDocumentListOut,
    KbDocumentOut,
    KbPreviewOut,
    KbPreviewPage,
    KbUploadOut,
)
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.storage import UploadRejected
from backend.doc_intel.textutil import truncate_utf8, utc_now
from backend.exceptions import BadRequestError, ConflictError, NotFoundError
from backend.services.audit import write_audit_log
from backend.services.error_log import persist_error_log
from backend.services.kb_queue_groups import all_queue_keys, validate_active_queues
from embedding_service.models.corpus_config import parse_corpus_config

_log = logging.getLogger(__name__)

MAX_QUEUES_PER_DOCUMENT = 50
LABEL_CACHE_SECONDS = 60.0
LABEL_LOOKUP_TIMEOUT_SECONDS = 3.0
HEARTBEAT_SECONDS = 60.0
_STATE_CHANGED = "The document changed state in the meantime; refresh and try again"

AuditFn = Callable[..., Awaitable[None]]
ErrorLogFn = Callable[..., Awaitable[None]]


class _Abandoned(Exception):
    """The document is no longer PROCESSING (recovered as interrupted); stop working on it."""


class KbImportService:
    def __init__(
        self,
        *,
        db: Any,
        repo: Any,
        kb: Any,
        settings: DocIntelSettings,
        embedder_factory: Callable[[dict[str, Any]], Any],
        extract_fn: Callable[..., NormalizedDocument] | None = None,
        storage: Any = None,
        audit: AuditFn | None = None,
        error_log: ErrorLogFn | None = None,
        default_price_per_million: float | None = None,
    ) -> None:
        self._db = db
        self.repo = repo
        self.kb = kb
        self.settings = settings
        self._embedder_factory = embedder_factory
        self._extract = extract_fn or extraction_module.run_extraction
        self._storage = storage or storage_module
        self._audit = audit or self._write_audit
        self._error_log = error_log or self._write_error_log
        self._default_price = default_price_per_million
        self._wake: Callable[[], None] | None = None
        self._config_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def set_wake_callback(self, wake: Callable[[], None] | None) -> None:
        self._wake = wake

    def wake_worker(self) -> None:
        if self._wake is not None:
            try:
                self._wake()
            except Exception:
                _log.warning("doc_intel: could not wake the import worker", exc_info=True)

    # ---- upload ---------------------------------------------------------------------------

    async def upload(
        self,
        user: UserContext,
        account_id: int,
        queue_keys: Sequence[str],
        files: Sequence[UploadFile],
    ) -> KbUploadOut:
        """Store each file and queue it; rejected files come back with upload FAILED + reason."""
        corpus_id, config = await self._account_corpus(account_id)
        keys = self._validate_queues(config, queue_keys)
        if not files:
            raise BadRequestError("Select at least one file")
        limit = self.settings.max_files_per_upload
        if len(files) > limit:
            raise BadRequestError(f"Too many files: {len(files)} (limit is {limit} per upload)")

        batch_id = uuid.uuid4().hex
        ids: list[int] = []
        accepted = 0
        for upload in files:
            doc_id, ok = await self._store_one(
                upload, batch_id=batch_id, account_id=account_id, corpus_id=corpus_id, queue_keys=keys, user=user
            )
            ids.append(doc_id)
            accepted += int(ok)
        if accepted:
            self.wake_worker()

        rows = [row for row in [await self.repo.get_document(i) for i in ids] if row]
        documents = await self._to_out(rows)
        await self._audit_safe(
            user,
            batch_id,
            "UPLOAD",
            new={
                "account_id": account_id,
                "queue_keys": keys,
                "documents": [{"id": d.id, "filename": d.filename, "status": d.status} for d in documents],
            },
        )
        return KbUploadOut(batch_id=batch_id, accepted=accepted, rejected=len(ids) - accepted, documents=documents)

    async def _store_one(
        self,
        upload: UploadFile,
        *,
        batch_id: str,
        account_id: int,
        corpus_id: str,
        queue_keys: list[str],
        user: UserContext,
    ) -> tuple[int, bool]:
        started = utc_now()
        filename = _display_filename(self._storage, getattr(upload, "filename", None))
        common = {"batch_id": batch_id, "account_id": account_id, "corpus_id": corpus_id, "queue_keys": queue_keys, "user": user}
        try:
            doc_dir = self._storage.new_document_dir(self.settings)
        except Exception as ex:
            _log.exception("doc_intel: could not create a storage directory")
            reason = f"Could not store the file on the server ({type(ex).__name__}) — check DOC_INTEL_STORAGE_DIR"
            return await self._insert_rejected(filename=filename, reason=reason, started=started, **common), False
        try:
            stored = await self._storage.save_upload(upload, doc_dir, settings=self.settings)
        except UploadRejected as ex:
            self._remove_dir(doc_dir)
            return await self._insert_rejected(filename=filename, reason=ex.reason, started=started, **common), False
        except Exception as ex:
            _log.exception("doc_intel: could not store upload %r", filename)
            self._remove_dir(doc_dir)
            reason = f"Could not store the file on the server ({type(ex).__name__})"
            return await self._insert_rejected(filename=filename, reason=reason, started=started, **common), False
        except BaseException:
            # Cancelled (client gone, shutdown): no row will ever point at this directory.
            self._remove_dir(doc_dir)
            raise

        try:
            duplicate = await self.repo.find_active_duplicate(corpus_id, stored.sha256)
        except BaseException:
            # Registry unavailable or request cancelled: never leave an unreferenced client file on disk.
            self._remove_dir(doc_dir)
            raise
        if duplicate is not None:
            self._remove_dir(doc_dir)
            reason = f"Duplicate of document #{duplicate}, which is already imported — use Change queues instead"
            return (
                await self._insert_rejected(
                    filename=stored.filename,
                    reason=reason,
                    started=started,
                    content_type=stored.content_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    **common,
                ),
                False,
            )

        details = apply_stage_change(
            {},
            StageChange(
                "upload",
                STAGE_COMPLETED,
                metrics={"size_bytes": stored.size_bytes, "kind": stored.kind},
                started_at=started,
            ),
            now=utc_now(),
        )
        try:
            doc_id = await self.repo.insert_document(
                batch_id=batch_id,
                account_id=account_id,
                corpus_id=corpus_id,
                queue_keys=queue_keys,
                filename=stored.filename,
                status=DOC_QUEUED,
                stage_statuses={"upload": STAGE_COMPLETED},
                content_type=stored.content_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                storage_dir=str(stored.doc_dir),
                uploaded_by=user.id,
                stage_details=details,
            )
        except BaseException:
            self._remove_dir(doc_dir)
            raise
        return doc_id, True

    async def _insert_rejected(
        self,
        *,
        filename: str,
        reason: str,
        started: Any,
        batch_id: str,
        account_id: int,
        corpus_id: str,
        queue_keys: list[str],
        user: UserContext,
        content_type: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
    ) -> int:
        details = apply_stage_change({}, StageChange("upload", STAGE_FAILED, error=reason, started_at=started), now=utc_now())
        statuses = {stage: STAGE_SKIPPED for stage in KB_STAGES}
        statuses["upload"] = STAGE_FAILED
        return await self.repo.insert_document(
            batch_id=batch_id,
            account_id=account_id,
            corpus_id=corpus_id,
            queue_keys=queue_keys,
            filename=filename,
            status=DOC_FAILED,
            stage_statuses=statuses,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            uploaded_by=user.id,
            failed_stage="upload",
            error_message=reason,
            stage_details=details,
            finished=True,
        )

    # ---- reads ----------------------------------------------------------------------------

    async def get_document_out(self, doc_id: int) -> KbDocumentOut:
        row = await self._require(doc_id)
        return (await self._to_out([row]))[0]

    async def list_documents(
        self,
        *,
        account_id: int | None = None,
        status: str | None = None,
        batch_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> KbDocumentListOut:
        rows, total = await self.repo.list_documents(
            account_id=account_id, status=status, batch_id=batch_id, limit=limit, offset=offset
        )
        return KbDocumentListOut(items=await self._to_out(rows), limit=limit, offset=offset, total=total)

    async def preview(self, doc_id: int, max_chars: int) -> KbPreviewOut:
        """Extracted text per page (from ``normalized.json``), cut at ``max_chars`` in total."""
        row = await self._require(doc_id)
        path = self._normalized_path(row)
        if path is None or not path.is_file():
            raise ConflictError("No extracted text yet — the preview is available once extraction has completed")
        try:
            doc = await asyncio.to_thread(NormalizedDocument.load, path)
        except Exception as ex:
            _log.warning("doc_intel: unreadable normalized.json for document %s", doc_id, exc_info=True)
            raise ConflictError("The extracted text could not be read; republish or upload the document again") from ex
        pages = [p for p in doc.pages if p.text]
        if not pages and doc.blocks:
            pages = [NormalizedPage(number=1, text="\n\n".join(b.text for b in doc.blocks if b.text))]
        remaining = max(0, int(max_chars))
        out: list[KbPreviewPage] = []
        truncated = False
        for page in pages:
            if remaining <= 0:
                truncated = True
                break
            text = page.text
            if len(text) > remaining:
                text = text[:remaining]
                truncated = True
            out.append(KbPreviewPage(number=page.number, text=text))
            remaining -= len(text)
        return KbPreviewOut(
            document_id=int(row["id"]),
            page_count=doc.page_count or len(doc.pages) or None,
            truncated=truncated,
            pages=out,
            extractor=dict(doc.extractor or {}),
        )

    # ---- admin actions --------------------------------------------------------------------

    async def retry(self, user: UserContext, doc_id: int) -> KbDocumentOut:
        """FAILED -> QUEUED, resuming at chunking when the extracted text is still on disk."""
        row = await self._require(doc_id)
        status = str(row.get("status"))
        if status != DOC_FAILED:
            raise ConflictError(f"Only failed documents can be retried (this one is {status})")
        if row.get("upload_status") == STAGE_FAILED:
            raise ConflictError("The upload failed; upload the file again")
        normalized = self._normalized_path(row)
        if row.get("extraction_status") == STAGE_COMPLETED and normalized is not None and normalized.is_file():
            from_stage = "chunking"
        elif _original_path(self._doc_dir(row), row) is not None:
            from_stage = "extraction"
        else:
            raise ConflictError("The uploaded file is no longer on the server; upload it again")
        if not await self.repo.reset_for_retry(doc_id, from_stage):
            raise ConflictError(_STATE_CHANGED)
        self.wake_worker()
        await self._audit_safe(
            user,
            doc_id,
            "RETRY",
            old={"status": status, "failed_stage": row.get("failed_stage")},
            new={"status": DOC_QUEUED, "from_stage": from_stage},
        )
        return await self.get_document_out(doc_id)

    async def republish(self, user: UserContext, doc_id: int) -> KbDocumentOut:
        """Re-chunk, re-embed and re-publish from the stored extracted text."""
        row = await self._require(doc_id)
        status = str(row.get("status"))
        extracted = row.get("extraction_status") == STAGE_COMPLETED
        if not (status == DOC_PUBLISHED or (status == DOC_FAILED and extracted)):
            raise ConflictError(
                f"Only published documents, or failed documents whose extraction completed, can be republished (this one is {status})"
            )
        normalized = self._normalized_path(row)
        if normalized is None or not normalized.is_file():
            raise ConflictError("The extracted text is no longer on the server; upload the file again")
        if not await self.repo.reset_for_republish(doc_id):
            raise ConflictError(_STATE_CHANGED)
        self.wake_worker()
        await self._audit_safe(user, doc_id, "REPUBLISH", old={"status": status}, new={"status": DOC_QUEUED})
        return await self.get_document_out(doc_id)

    async def unpublish(self, user: UserContext, doc_id: int) -> KbDocumentOut:
        """Remove the document from every queue, then delete its chunks; the row stays (UNPUBLISHED)."""
        row = await self._require(doc_id)
        status = str(row.get("status"))
        if status in (DOC_QUEUED, DOC_PROCESSING):
            raise ConflictError("The document is still being imported; unpublish it once it has finished")
        if status == DOC_UNPUBLISHED:
            return await self.get_document_out(doc_id)
        try:
            summary = await asyncio.to_thread(
                self.kb.unpublish, corpus_id_hex=str(row.get("corpus_id")), vertical=_vertical(row)
            )
        except PublishFailed as ex:
            raise ServiceUnavailableError(ex.reason) from ex
        except Exception as ex:
            _log.warning("doc_intel: unpublish of document %s failed", doc_id, exc_info=True)
            raise ServiceUnavailableError(f"Knowledge base database unavailable: {_first_line(ex)}") from ex
        if not await self.repo.mark_unpublished(doc_id):
            raise ConflictError(_STATE_CHANGED)
        await self._audit_safe(
            user,
            doc_id,
            "UNPUBLISH",
            old={"status": status},
            new={
                "status": DOC_UNPUBLISHED,
                "removed_from_queues": summary.get("removed_from_queues", []),
                "chunks_deleted": summary.get("chunks_deleted", 0),
            },
        )
        return await self.get_document_out(doc_id)

    async def change_queues(self, user: UserContext, doc_id: int, queue_keys: Sequence[str]) -> KbDocumentOut:
        """PUBLISHED: config-only KB change (no re-embedding). QUEUED/FAILED: the row only."""
        row = await self._require(doc_id)
        status = str(row.get("status"))
        if status == DOC_PROCESSING:
            raise ConflictError("The document is being imported right now; change its queues once it has finished")
        if status == DOC_UNPUBLISHED:
            raise ConflictError("The document is unpublished; upload it again to import it")
        corpus_id = str(row.get("corpus_id"))
        config = await self._load_config(corpus_id)
        if config is None:
            raise ConflictError("Knowledge base corpus not found")
        keys = self._validate_queues(config, queue_keys)
        old_keys = parse_queue_keys(row.get("queue_keys"))
        if status == DOC_PUBLISHED:
            try:
                await asyncio.to_thread(self.kb.set_queues, corpus_id_hex=corpus_id, vertical=_vertical(row), queue_keys=keys)
            except PublishFailed as ex:
                if ex.is_database_error:
                    raise ServiceUnavailableError(ex.reason) from ex
                raise ConflictError(ex.reason) from ex
            except Exception as ex:
                _log.warning("doc_intel: queue change of document %s failed", doc_id, exc_info=True)
                raise ServiceUnavailableError(f"Knowledge base database unavailable: {_first_line(ex)}") from ex
            ok = await self.repo.update_queue_keys(doc_id, keys, statuses=(DOC_PUBLISHED,))
        else:
            ok = await self.repo.update_queue_keys(doc_id, keys, statuses=(DOC_QUEUED, DOC_FAILED))
        if not ok:
            raise ConflictError(_STATE_CHANGED)
        self._config_cache.pop(corpus_id, None)
        await self._audit_safe(user, doc_id, "CHANGE_QUEUES", old={"queue_keys": old_keys}, new={"queue_keys": keys})
        return await self.get_document_out(doc_id)

    # ---- pipeline (worker) ----------------------------------------------------------------

    async def process_document(self, row: Mapping[str, Any]) -> str:
        """Run the remaining stages of a claimed (PROCESSING) document; returns the final status.

        Never raises (except on cancellation): known failures mark their stage FAILED
        with an admin-readable reason; anything else is recorded as "Unexpected error".
        """
        doc_id = int(row["id"])
        state = {"stage": "extraction"}
        try:
            return await self._pipeline(row, state)
        except _Abandoned:
            _log.warning("doc_intel: document %s is no longer PROCESSING; abandoned", doc_id)
            return "ABANDONED"
        except Exception as ex:
            stage = state["stage"]
            reason = truncate_utf8(f"Unexpected error: {type(ex).__name__}: {scrub_secrets(str(ex))}", MAX_ERROR_BYTES)
            _log.exception("doc_intel: document %s failed at %s", doc_id, stage)
            try:
                await self.repo.finish(doc_id, status=DOC_FAILED, stage=stage, error=reason)
            except Exception:
                _log.exception("doc_intel: could not record the failure of document %s", doc_id)
            await self._error_log_safe(ex, doc_id)
            return DOC_FAILED

    async def _pipeline(self, row: Mapping[str, Any], state: dict[str, str]) -> str:
        doc_id = int(row["id"])
        corpus_id = str(row.get("corpus_id") or "")
        vertical = _vertical(row)
        queue_keys = parse_queue_keys(row.get("queue_keys"))
        filename = str(row.get("filename") or f"document-{doc_id}")
        doc_dir = self._doc_dir(row)

        # 1. extraction (skipped when a previous run already produced normalized.json)
        state["stage"] = "extraction"
        doc: NormalizedDocument | None = None
        normalized = self._storage.normalized_path(doc_dir) if doc_dir is not None else None
        if row.get("extraction_status") == STAGE_COMPLETED and normalized is not None and normalized.is_file():
            try:
                doc = await asyncio.to_thread(NormalizedDocument.load, normalized)
            except Exception:
                _log.warning("doc_intel: normalized.json of document %s unreadable; extracting again", doc_id, exc_info=True)
        if doc is None:
            await self._stage(doc_id, "extraction", STAGE_RUNNING)
            original = _original_path(doc_dir, row)
            if original is None or normalized is None:
                return await self._fail(doc_id, "extraction", "The uploaded file is missing from server storage — upload it again")
            try:
                doc = await asyncio.to_thread(
                    self._extract,
                    original,
                    filename=filename,
                    media_type=str(row.get("content_type") or ""),
                    sha256=str(row.get("sha256") or ""),
                    out_path=normalized,
                    settings=self.settings,
                )
            except ExtractionFailed as ex:
                return await self._fail(doc_id, "extraction", ex.reason)
            await self._stage(doc_id, "extraction", STAGE_COMPLETED, metrics=_extraction_metrics(doc))
            await self._results(
                doc_id,
                page_count=doc.page_count or None,
                warnings=[w.model_dump() for w in doc.warnings],
            )

        # 2. chunking (sizes from the corpus config)
        state["stage"] = "chunking"
        await self._stage(doc_id, "chunking", STAGE_RUNNING)
        try:
            config = await asyncio.to_thread(self.kb.get_corpus_config, corpus_id)
        except Exception as ex:
            return await self._fail(doc_id, "chunking", f"Knowledge base database unavailable: {_first_line(ex)}")
        if config is None:
            return await self._fail(doc_id, "chunking", "Knowledge base corpus not found")
        try:
            cfg = parse_corpus_config(config)
        except Exception as ex:
            return await self._fail(doc_id, "chunking", f"The knowledge base configuration is invalid: {_first_line(ex)}")
        try:
            chunks = await asyncio.to_thread(
                build_chunks,
                doc,
                document_id=doc_id,
                filename=filename,
                queue_keys=queue_keys,
                max_chars=cfg.chunk_max_chars,
                overlap=cfg.chunk_overlap,
                max_chunks=self.settings.max_chunks,
            )
        except ChunkingFailed as ex:
            return await self._fail(doc_id, "chunking", ex.reason)
        total_chars = sum(len(c.text) for c in chunks)
        await self._stage(
            doc_id,
            "chunking",
            STAGE_COMPLETED,
            metrics={
                "chunks": len(chunks),
                "max_chars": cfg.chunk_max_chars,
                "overlap": cfg.chunk_overlap,
                "avg_chars": round(total_chars / len(chunks)) if chunks else 0,
            },
        )
        await self._results(doc_id, chunk_count=len(chunks))

        # 3. embedding (HTTP; no DB connection held)
        state["stage"] = "embedding"
        await self._stage(doc_id, "embedding", STAGE_RUNNING)
        try:
            embedder = self._embedder_factory(config)
        except Exception as ex:
            return await self._fail(
                doc_id, "embedding", f"The corpus embedder is not configured correctly: {scrub_secrets(_first_line(ex))}"
            )
        try:
            outcome = await asyncio.to_thread(
                embed_texts,
                [c.text for c in chunks],
                embedder=embedder,
                batch_size=self.settings.embed_batch_size,
                max_retries=self.settings.embed_max_retries,
                conn_factory=getattr(self.kb, "connection_factory", None),
            )
        except EmbeddingFailed as ex:
            return await self._fail(doc_id, "embedding", ex.reason)
        tokens = outcome.tokens
        price = cfg.embedder.pricing_usd_per_million_tokens
        if price is None:
            price = self._default_price
        cost = round(tokens / 1_000_000 * float(price), 6) if price is not None else None
        await self._stage(
            doc_id,
            "embedding",
            STAGE_COMPLETED,
            metrics={
                "model": cfg.embedder.model,
                "dimension": cfg.embedder.dimension,
                "batches": outcome.batches,
                "tokens": tokens,
                "tokens_source": "provider" if outcome.api_tokens is not None else "estimate",
                "price_usd_per_million_tokens": price,
                "cost_usd": cost,
            },
        )
        await self._results(doc_id, tokens_used=tokens, cost_usd=cost)

        # 4. publishing (one KB transaction: chunks + vectors + queue verticals)
        state["stage"] = "publishing"
        await self._stage(doc_id, "publishing", STAGE_RUNNING)
        if not queue_keys:
            return await self._fail(doc_id, "publishing", "No queue is selected for this document — use Change queues, then Retry")
        try:
            summary = await asyncio.to_thread(
                self.kb.publish,
                corpus_id_hex=corpus_id,
                vertical=vertical,
                queue_keys=queue_keys,
                chunks=chunks,
                vectors=outcome.vectors,
                embedding_model=cfg.embedder.model,
                dimension=cfg.embedder.dimension,
            )
        except PublishFailed as ex:
            return await self._fail(doc_id, "publishing", ex.reason)
        finished = await self.repo.finish(
            doc_id,
            status=DOC_PUBLISHED,
            metrics=summary,
            page_count=doc.page_count or None,
            chunk_count=len(chunks),
            tokens_used=tokens,
            cost_usd=cost,
        )
        if not finished:
            raise _Abandoned()
        self._config_cache.pop(corpus_id, None)
        _log.info("doc_intel: document %s published (%s chunks)", doc_id, len(chunks))
        return DOC_PUBLISHED

    async def _stage(self, doc_id: int, stage: str, status: str, *, metrics: Mapping[str, Any] | None = None) -> None:
        if not await self.repo.set_stage(doc_id, stage, status, metrics=metrics):
            raise _Abandoned()

    async def _results(self, doc_id: int, **fields: Any) -> None:
        if not await self.repo.set_results(doc_id, **fields):
            raise _Abandoned()

    async def _fail(self, doc_id: int, stage: str, reason: str) -> str:
        _log.info("doc_intel: document %s failed at %s: %s", doc_id, stage, reason)
        if not await self.repo.finish(doc_id, status=DOC_FAILED, stage=stage, error=reason):
            raise _Abandoned()
        return DOC_FAILED

    # ---- helpers --------------------------------------------------------------------------

    async def _require(self, doc_id: int) -> dict[str, Any]:
        row = await self.repo.get_document(doc_id)
        if not row:
            raise NotFoundError("Document not found")
        return row

    async def _account_corpus(self, account_id: int) -> tuple[str, dict[str, Any]]:
        account = await self.repo.get_account(account_id)
        if not account:
            raise NotFoundError("Account not found")
        raw = account.get("corpus_id")
        if not raw:
            raise BadRequestError("Account has no knowledge base (corpus_id) configured")
        corpus_id = normalize_corpus_id(raw)
        if corpus_id is None:
            raise BadRequestError("Account has an invalid knowledge base id (corpus_id)")
        config = await self._load_config(corpus_id)
        if config is None:
            raise BadRequestError("Knowledge base corpus not found")
        return corpus_id, config

    async def _load_config(self, corpus_id: str) -> dict[str, Any] | None:
        try:
            config = await asyncio.to_thread(self.kb.get_corpus_config, corpus_id)
        except Exception as ex:
            _log.warning("doc_intel: could not read corpus %s config", corpus_id, exc_info=True)
            raise ServiceUnavailableError(f"Knowledge base database unavailable: {_first_line(ex)}") from ex
        self._config_cache[corpus_id] = (time.monotonic(), config)
        return config

    def _validate_queues(self, config: dict[str, Any], queue_keys: Sequence[str] | None) -> list[str]:
        requested = [str(k) for k in (queue_keys or [])]
        if len(requested) > MAX_QUEUES_PER_DOCUMENT:
            raise BadRequestError(f"Too many queues selected (limit is {MAX_QUEUES_PER_DOCUMENT})")
        keys = validate_active_queues(config, requested, allowed_queue_keys=all_queue_keys(config))
        if not keys:
            raise BadRequestError("Select at least one queue")
        return keys

    async def _labels_config(self, corpus_id: str) -> dict[str, Any] | None:
        """Corpus config for queue labels: cached ~60 s, best-effort (None when unavailable)."""
        cached = self._config_cache.get(corpus_id)
        if cached is not None and time.monotonic() - cached[0] < LABEL_CACHE_SECONDS:
            return cached[1]
        try:
            config = await asyncio.wait_for(
                asyncio.to_thread(self.kb.get_corpus_config, corpus_id), LABEL_LOOKUP_TIMEOUT_SECONDS
            )
        except Exception:
            _log.debug("doc_intel: queue labels unavailable for corpus %s", corpus_id, exc_info=True)
            config = None
        self._config_cache[corpus_id] = (time.monotonic(), config)
        return config

    async def _to_out(self, rows: Sequence[Mapping[str, Any]]) -> list[KbDocumentOut]:
        if not rows:
            return []
        positions = await self.repo.queue_positions() if any(r.get("status") == DOC_QUEUED for r in rows) else {}
        configs: dict[str, dict[str, Any] | None] = {}
        for corpus_id in {str(r.get("corpus_id") or "") for r in rows}:
            configs[corpus_id] = await self._labels_config(corpus_id) if corpus_id else None
        out: list[KbDocumentOut] = []
        for r in rows:
            config = configs.get(str(r.get("corpus_id") or ""))
            labels = queue_labels(config, parse_queue_keys(r.get("queue_keys"))) if config is not None else None
            out.append(row_to_out(r, queue_labels=labels, queue_position=positions.get(int(r["id"]))))
        return out

    def _doc_dir(self, row: Mapping[str, Any]) -> Path | None:
        raw = row.get("storage_dir")
        return Path(str(raw)) if raw else None

    def _normalized_path(self, row: Mapping[str, Any]) -> Path | None:
        doc_dir = self._doc_dir(row)
        return self._storage.normalized_path(doc_dir) if doc_dir is not None else None

    def _remove_dir(self, doc_dir: Path) -> None:
        try:
            self._storage.remove_document_dir(doc_dir, settings=self.settings)
        except Exception:
            _log.warning("doc_intel: could not remove %s", doc_dir, exc_info=True)

    async def _audit_safe(
        self,
        user: UserContext,
        entity_id: str | int,
        action: str,
        *,
        old: Any = None,
        new: Any = None,
    ) -> None:
        if not self.settings.audit_enabled:
            return
        try:
            await self._audit(user_id=user.id, entity_id=entity_id, action_type=action, old_value=old, new_value=new)
        except Exception:
            _log.warning("doc_intel: audit %s %s not written", action, entity_id, exc_info=True)

    async def _write_audit(self, *, user_id: int | None, entity_id: str | int, action_type: str, old_value: Any, new_value: Any) -> None:
        if self._db is None:
            return
        await write_audit_log(
            self._db,
            user_id=user_id,
            entity_type="kb_document",
            entity_id=entity_id,
            action_type=action_type,
            old_value=old_value,
            new_value=new_value,
        )

    async def _error_log_safe(self, ex: BaseException, doc_id: int) -> None:
        if not self.settings.error_log_enabled:
            return
        try:
            await self._error_log(
                exception_type=type(ex).__name__,
                exception_message=truncate_utf8(scrub_secrets(str(ex)), 2000),
                # The traceback repeats the exception text (and that of any chained cause):
                # scrub it too, or the message scrubbing above is pointless.
                stack_trace=scrub_secrets("".join(traceback.format_exception(type(ex), ex, ex.__traceback__))),
                path=f"/api/doc-intel/kb-documents/{doc_id}",
            )
        except Exception:
            _log.warning("doc_intel: error log not written", exc_info=True)

    async def _write_error_log(self, *, exception_type: str, exception_message: str | None, stack_trace: str | None, path: str | None) -> None:
        # AIVA_error_logs shows rows WITHOUT an org to every org-scoped viewer (org admins,
        # account managers, supervisors), and these rows name documents and carry a
        # traceback. So they are only stored when they can be scoped to the platform org;
        # otherwise the failure stays in the server log and the Super Admin/Developer-only
        # Monitoring view (plan review finding F4).
        org_id = get_backend_settings().notify_platform_org_id
        if org_id is None:
            _log.warning(
                "doc_intel: %s not written to AIVA_error_logs (set NOTIFY_PLATFORM_ORG_ID to scope it)", exception_type
            )
            return
        await persist_error_log(
            self._db,
            exception_type=exception_type,
            exception_message=exception_message,
            stack_trace=stack_trace,
            source="DOC_INTEL",
            path=path,
            route_template="doc_intel.process_document",
            org_id=int(org_id),
        )


class KbImportWorker:
    """Background loop: recover interrupted rows, claim the next QUEUED document, process it."""

    def __init__(
        self,
        service: KbImportService,
        settings: DocIntelSettings,
        worker_id: str | None = None,
        *,
        heartbeat_seconds: float = HEARTBEAT_SECONDS,
    ) -> None:
        self._service = service
        self._settings = settings
        self.worker_id = (worker_id or default_worker_id())[:128]
        self._heartbeat_seconds = heartbeat_seconds
        self._stale_after = timedelta(seconds=max(300.0, 2 * heartbeat_seconds))
        self._task: asyncio.Task[None] | None = None
        self._wake_event: asyncio.Event | None = None
        self._stopping = False
        self._current_doc: int | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start the loop on the running event loop (idempotent)."""
        if self.running:
            return
        self._stopping = False
        self._wake_event = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._run(), name="doc-intel-import-worker")
        _log.info("doc_intel: import worker %s started", self.worker_id)

    def wake(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()

    async def stop(self, timeout: float = 10.0) -> None:
        """Cancel the loop; the document in flight is marked interrupted (retryable)."""
        self._stopping = True
        task, current = self._task, self._current_doc
        self._task = None
        if task is None:
            return
        self.wake()
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout)
        except Exception:
            _log.warning("doc_intel: import worker did not stop within %ss", timeout)
        if current is not None:
            try:
                await asyncio.wait_for(self._service.repo.release_interrupted(current, self.worker_id), 5)
            except Exception:
                _log.warning("doc_intel: could not release document %s", current, exc_info=True)

    async def _run(self) -> None:
        repo = self._service.repo
        while not self._stopping:
            try:
                await repo.recover_stale(utc_now() - self._stale_after)
                while not self._stopping:
                    row = await repo.claim_next(self.worker_id)
                    if row is None:
                        break
                    await self._process(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("doc_intel: import worker poll failed")
            await self._idle()

    async def _process(self, row: Mapping[str, Any]) -> None:
        doc_id = int(row["id"])
        self._current_doc = doc_id
        heartbeat = asyncio.create_task(self._heartbeat(doc_id), name=f"doc-intel-heartbeat-{doc_id}")
        try:
            await self._service.process_document(row)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self._current_doc = None

    async def _heartbeat(self, doc_id: int) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            try:
                await self._service.repo.touch(doc_id, self.worker_id)
            except Exception:
                _log.warning("doc_intel: heartbeat for document %s failed", doc_id, exc_info=True)

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
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"[:128]


# ---- module helpers ------------------------------------------------------------------------


def _vertical(row: Mapping[str, Any]) -> str:
    return str(row.get("vertical") or kb_vertical_for(int(row["id"])))


def _first_line(ex: BaseException) -> str:
    text = str(ex).strip()
    return (text.splitlines()[0] if text else type(ex).__name__)[:300]


def _display_filename(storage: Any, name: str | None) -> str:
    try:
        return str(storage.sanitize_filename(name))[:MAX_FILENAME_CHARS] or "upload"
    except Exception:
        base = os.path.basename(str(name or "").replace("\\", "/")).strip()
        cleaned = "".join(ch for ch in base if ch.isprintable())
        return (cleaned or "upload")[:MAX_FILENAME_CHARS]


def _original_path(doc_dir: Path | None, row: Mapping[str, Any]) -> Path | None:
    """``<doc_dir>/original.<ext>`` of a stored upload, or None when it is gone."""
    if doc_dir is None:
        return None
    by_type = {mime: ext for ext, mime in ALLOWED_EXTENSIONS.items()}
    candidates: list[str] = []
    ext = by_type.get(str(row.get("content_type") or ""))
    if ext:
        candidates.append(ext)
    suffix = Path(str(row.get("filename") or "")).suffix.lower()
    if suffix in ALLOWED_EXTENSIONS and suffix not in candidates:
        candidates.append(suffix)
    candidates += [e for e in ALLOWED_EXTENSIONS if e not in candidates]
    for ext in candidates:
        path = doc_dir / f"original{ext}"
        if path.is_file():
            return path
    return None


def _extraction_metrics(doc: NormalizedDocument) -> dict[str, Any]:
    info = doc.extractor or {}
    metrics: dict[str, Any] = {
        "pages": doc.page_count,
        "blocks": len(doc.blocks),
        "text_chars": doc.text_chars,
        "warnings": len(doc.warnings),
    }
    for key in ("name", "version", "mode", "pdf_engine", "fallback", "seconds"):
        value = info.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            if value is not None:
                metrics[f"extractor_{key}"] = value
    return metrics
