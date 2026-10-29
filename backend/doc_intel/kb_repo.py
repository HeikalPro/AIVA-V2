"""Async SQL for ``AIVA_kb_documents`` (the document registry AND the import work queue).

All SQL uses bind variables; stage columns are only ever taken from a whitelist.
Every timestamp written here is naive UTC (``textutil.utc_now()``), and every one read
back is rendered with ``textutil.iso_utc`` (ISO-8601 + ``Z``).

``AIVA_accounts`` / ``AIVA_organizations`` / ``AIVA_users`` are only read (joins).
Stage bookkeeping (``stage_details`` JSON) is done by the pure helpers at the top so the
same rules apply wherever a row changes.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import Database
from backend.doc_intel.constants import (
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_PUBLISHED,
    DOC_QUEUED,
    DOC_UNPUBLISHED,
    KB_STAGES,
    MAX_ERROR_BYTES,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
    STAGE_RUNNING,
    STAGE_SKIPPED,
    kb_vertical_for,
)
from backend.doc_intel.queue_config import to_plain
from backend.doc_intel.schemas import DocWarningOut, KbDocumentOut, StageOut
from backend.doc_intel.textutil import iso_utc, truncate_utf8, utc_now

_log = logging.getLogger(__name__)

T = "AIVA_kb_documents"
INTERRUPTED_REASON = "Interrupted (server restart or crash) — use Retry"
MAX_FILENAME_BYTES = 1024  # VARCHAR2(1024) in V001
_CLAIM_ATTEMPTS = 3

_STAGE_COLUMNS: dict[str, str] = {stage: f"{stage}_status" for stage in KB_STAGES}
# Columns the generic update may set (anything else is a programming error).
_WRITABLE_COLUMNS = frozenset(
    {
        "status",
        "failed_stage",
        "error_message",
        "page_count",
        "chunk_count",
        "tokens_used",
        "cost_usd",
        "warnings_json",
        "queue_keys",
        "started_at",
        "finished_at",
        "published_at",
    }
)


class _Now:
    """Column value placeholder: "the timestamp of this update"."""


NOW = _Now()
_UNSET: Any = object()


# ---- pure helpers --------------------------------------------------------------------------


def stage_column(stage: str) -> str:
    try:
        return _STAGE_COLUMNS[stage]
    except KeyError:
        raise ValueError(f"Unknown stage: {stage!r}") from None


def loads_json(value: Any, default: Any) -> Any:
    """Parse a JSON CLOB value; ``default`` when empty, invalid or of the wrong shape."""
    if value is None or value == "":
        return default
    parsed = value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return default
    return parsed if isinstance(parsed, type(default)) else default


def dumps_json(value: Any) -> str:
    return json.dumps(to_plain(value), ensure_ascii=False, default=str)


def parse_utc(value: Any) -> datetime | None:
    """A stored/ISO timestamp as a naive UTC datetime (``Z`` / offsets accepted)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1]
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


@dataclass(frozen=True)
class StageChange:
    stage: str
    status: str
    error: str | None = None
    metrics: Mapping[str, Any] | None = None
    started_at: datetime | None = None


def apply_stage_change(details: Mapping[str, Any] | None, change: StageChange, *, now: datetime) -> dict[str, Any]:
    """New ``stage_details`` after ``change``.

    RUNNING starts a fresh entry; COMPLETED/FAILED stamp ``finished_at``, ``seconds``,
    ``metrics`` (and ``error``); PENDING/SKIPPED drop the entry.
    """
    stage_column(change.stage)
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


def interrupted_stage(row: Mapping[str, Any]) -> str:
    """The stage to mark FAILED for an interrupted PROCESSING document."""
    for stage in KB_STAGES:
        if row.get(stage_column(stage)) == STAGE_RUNNING:
            return stage
    for stage in KB_STAGES:
        if row.get(stage_column(stage)) in (STAGE_PENDING, STAGE_FAILED):
            return stage
    return KB_STAGES[-1]


def normalize_corpus_id(value: Any) -> str | None:
    """``AIVA_accounts.corpus_id`` (hex, maybe dashed) -> 32 lowercase hex chars, or None."""
    text = str(value or "").replace("-", "").strip().lower()
    if len(text) != 32:
        return None
    try:
        bytes.fromhex(text)
    except ValueError:
        return None
    return text


def parse_queue_keys(value: Any) -> list[str]:
    keys = loads_json(value, [])
    return [str(k) for k in keys if str(k).strip()]


def row_to_out(
    row: Mapping[str, Any],
    *,
    queue_labels: list[str] | None = None,
    queue_position: int | None = None,
) -> KbDocumentOut:
    """API model for a registry row: always the five stages, in pipeline order."""
    details = loads_json(row.get("stage_details"), {})
    failed_stage = row.get("failed_stage") if row.get("failed_stage") in KB_STAGES else None
    stages: list[StageOut] = []
    for stage in KB_STAGES:
        status = str(row.get(stage_column(stage)) or STAGE_PENDING)
        entry = details.get(stage) if isinstance(details.get(stage), dict) else {}
        error = entry.get("error") if status == STAGE_FAILED else None
        if status == STAGE_FAILED and not error and failed_stage == stage:
            error = row.get("error_message")
        stages.append(
            StageOut(
                name=stage,
                status=status,
                error=error,
                started_at=iso_utc(entry.get("started_at")),
                finished_at=iso_utc(entry.get("finished_at")),
            )
        )

    warnings: list[DocWarningOut] = []
    for item in loads_json(row.get("warnings_json"), []):
        if isinstance(item, dict) and item.get("message"):
            page = item.get("page")
            warnings.append(
                DocWarningOut(
                    code=str(item.get("code") or "warning"),
                    message=str(item.get("message")),
                    page=int(page) if isinstance(page, (int, float)) else None,
                )
            )

    keys = parse_queue_keys(row.get("queue_keys"))
    labels = list(queue_labels) if queue_labels is not None and len(queue_labels) == len(keys) else list(keys)
    status = str(row.get("status"))
    return KbDocumentOut(
        id=int(row["id"]),
        batch_id=row.get("batch_id"),
        account_id=int(row["account_id"]),
        account_name=row.get("account_name"),
        organization_name=row.get("organization_name"),
        corpus_id=str(row.get("corpus_id") or ""),
        queue_keys=keys,
        queue_labels=labels,
        vertical=row.get("vertical") or kb_vertical_for(int(row["id"])),
        filename=str(row.get("filename") or ""),
        content_type=row.get("content_type"),
        size_bytes=_int(row.get("size_bytes")),
        sha256=row.get("sha256"),
        status=status,
        stages=stages,
        failed_stage=failed_stage,
        error_message=row.get("error_message"),
        warnings=warnings,
        page_count=_int(row.get("page_count")),
        chunk_count=_int(row.get("chunk_count")),
        tokens_used=_int(row.get("tokens_used")),
        cost_usd=float(row["cost_usd"]) if row.get("cost_usd") is not None else None,
        attempts=_int(row.get("attempts")) or 0,
        queue_position=queue_position if status == DOC_QUEUED else None,
        uploaded_by=_int(row.get("uploaded_by")),
        uploaded_by_email=row.get("uploaded_by_email"),
        created_at=iso_utc(row.get("created_at")),
        updated_at=iso_utc(row.get("updated_at")),
        started_at=iso_utc(row.get("started_at")),
        finished_at=iso_utc(row.get("finished_at")),
        published_at=iso_utc(row.get("published_at")),
    )


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---- repository ----------------------------------------------------------------------------

_DOC_SELECT = f"""
    SELECT d.id, d.batch_id, d.account_id, d.corpus_id, d.queue_keys, d.vertical, d.filename,
           d.content_type, d.size_bytes, d.sha256, d.storage_dir, d.status,
           d.upload_status, d.extraction_status, d.chunking_status, d.embedding_status,
           d.publishing_status, d.failed_stage, d.error_message, d.stage_details, d.warnings_json,
           d.page_count, d.chunk_count, d.tokens_used, d.cost_usd, d.attempts, d.worker_id,
           d.uploaded_by, d.created_at, d.updated_at, d.started_at, d.finished_at, d.published_at,
           a.name AS account_name, o.name AS organization_name, u.email AS uploaded_by_email
    FROM {T} d
    LEFT JOIN AIVA_accounts a ON a.id = d.account_id
    LEFT JOIN AIVA_organizations o ON o.id = a.organization_id
    LEFT JOIN AIVA_users u ON u.id = d.uploaded_by
"""


class KbRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ---- accounts (read-only) -----------------------------------------------------------

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(
            "SELECT id, name, corpus_id FROM AIVA_accounts WHERE id = :id", {"id": account_id}
        )

    async def distinct_account_corpora(self) -> list[str]:
        rows = await self._db.fetch_all("SELECT DISTINCT corpus_id FROM AIVA_accounts WHERE corpus_id IS NOT NULL")
        out: list[str] = []
        for row in rows:
            cid = normalize_corpus_id(row.get("corpus_id"))
            if cid and cid not in out:
                out.append(cid)
        return out

    # ---- documents ----------------------------------------------------------------------

    async def insert_document(
        self,
        *,
        batch_id: str,
        account_id: int,
        corpus_id: str,
        queue_keys: Sequence[str],
        filename: str,
        status: str,
        stage_statuses: Mapping[str, str],
        content_type: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        storage_dir: str | None = None,
        uploaded_by: int | None = None,
        failed_stage: str | None = None,
        error_message: str | None = None,
        stage_details: Mapping[str, Any] | None = None,
        finished: bool = False,
    ) -> int:
        """Insert a registry row and set its vertical (``kbdoc-<id>``); returns the id."""
        now = utc_now()
        params: dict[str, Any] = {
            "batch_id": batch_id,
            "account_id": int(account_id),
            "corpus_id": corpus_id,
            "queue_keys": dumps_json(list(queue_keys)),
            "filename": truncate_utf8(filename, MAX_FILENAME_BYTES),
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "storage_dir": storage_dir,
            "status": status,
            "failed_stage": failed_stage,
            "error_message": truncate_utf8(error_message, MAX_ERROR_BYTES),
            "stage_details": dumps_json(dict(stage_details or {})),
            "uploaded_by": uploaded_by,
            "now": now,
            "finished_at": now if finished else None,
        }
        for stage in KB_STAGES:
            params[stage_column(stage)] = stage_statuses.get(stage, STAGE_PENDING)
        async with self._db.connection() as conn:
            doc_id = await self._db.execute(
                f"""
                INSERT INTO {T} (
                    batch_id, account_id, corpus_id, queue_keys, filename, content_type,
                    size_bytes, sha256, storage_dir, status,
                    upload_status, extraction_status, chunking_status, embedding_status, publishing_status,
                    failed_stage, error_message, stage_details, uploaded_by,
                    created_at, updated_at, finished_at
                ) VALUES (
                    :batch_id, :account_id, :corpus_id, :queue_keys, :filename, :content_type,
                    :size_bytes, :sha256, :storage_dir, :status,
                    :upload_status, :extraction_status, :chunking_status, :embedding_status, :publishing_status,
                    :failed_stage, :error_message, :stage_details, :uploaded_by,
                    :now, :now, :finished_at
                ) RETURNING id INTO :out_id
                """,
                params,
                conn=conn,
                return_id=True,
            )
            assert doc_id is not None
            await self._db.execute(
                f"UPDATE {T} SET vertical = :vertical WHERE id = :id",
                {"vertical": kb_vertical_for(doc_id), "id": doc_id},
                conn=conn,
            )
        return int(doc_id)

    async def get_document(self, doc_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one(f"{_DOC_SELECT} WHERE d.id = :id", {"id": int(doc_id)})

    async def list_documents(
        self,
        *,
        account_id: int | None = None,
        status: str | None = None,
        batch_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        filters: list[str] = []
        binds: dict[str, Any] = {}
        if account_id is not None:
            filters.append("d.account_id = :account_id")
            binds["account_id"] = int(account_id)
        if status:
            filters.append("d.status = :status")
            binds["status"] = status
        if batch_id:
            filters.append("d.batch_id = :batch_id")
            binds["batch_id"] = batch_id
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = await self._db.fetch_all(
            f"{_DOC_SELECT} {where} ORDER BY d.created_at DESC, d.id DESC OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY",
            {**binds, "offset": max(0, int(offset)), "limit": max(1, int(limit))},
        )
        total = await self._db.fetch_one(f"SELECT COUNT(*) AS total FROM {T} d {where}", binds)
        return rows, int((total or {}).get("total") or 0)

    async def queue_positions(self) -> dict[int, int]:
        """1-based position of every QUEUED document, in claim order."""
        rows = await self._db.fetch_all(f"SELECT id FROM {T} WHERE status = 'QUEUED' ORDER BY created_at, id")
        return {int(r["id"]): i + 1 for i, r in enumerate(rows)}

    async def claim_next(self, worker_id: str) -> dict[str, Any] | None:
        """Claim the oldest QUEUED document (conditional UPDATE, safe across processes)."""
        for _ in range(_CLAIM_ATTEMPTS):
            row = await self._db.fetch_one(
                f"SELECT id FROM {T} WHERE status = 'QUEUED' ORDER BY created_at, id FETCH FIRST 1 ROWS ONLY"
            )
            if row is None:
                return None
            doc_id = int(row["id"])
            async with self._db.connection() as conn:
                cur = conn.cursor()
                await cur.execute(
                    f"""
                    UPDATE {T}
                    SET status = 'PROCESSING', attempts = attempts + 1, worker_id = :worker_id,
                        started_at = :now, finished_at = NULL, updated_at = :now
                    WHERE id = :id AND status = 'QUEUED'
                    """,
                    {"worker_id": worker_id[:128], "now": utc_now(), "id": doc_id},
                )
                claimed = cur.rowcount == 1
            if claimed:
                return await self.get_document(doc_id)
        return None

    async def set_stage(
        self,
        doc_id: int,
        stage: str,
        status: str,
        *,
        error: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        started_at: datetime | None = None,
    ) -> bool:
        """Update one stage of a PROCESSING document; False when it is no longer PROCESSING."""
        columns: dict[str, Any] = {}
        if status == STAGE_FAILED:
            columns["failed_stage"] = stage
            columns["error_message"] = truncate_utf8(error or "Failed", MAX_ERROR_BYTES)
        return await self._update(
            doc_id,
            stages=[StageChange(stage, status, error=error, metrics=metrics, started_at=started_at)],
            columns=columns,
            expect_status=(DOC_PROCESSING,),
        )

    async def set_results(
        self,
        doc_id: int,
        *,
        page_count: Any = _UNSET,
        chunk_count: Any = _UNSET,
        tokens_used: Any = _UNSET,
        cost_usd: Any = _UNSET,
        warnings: Any = _UNSET,
    ) -> bool:
        columns: dict[str, Any] = {}
        for name, value in (
            ("page_count", page_count),
            ("chunk_count", chunk_count),
            ("tokens_used", tokens_used),
            ("cost_usd", cost_usd),
        ):
            if value is not _UNSET:
                columns[name] = value
        if warnings is not _UNSET:
            columns["warnings_json"] = dumps_json(list(warnings or []))
        if not columns:
            return True
        return await self._update(doc_id, stages=[], columns=columns, expect_status=(DOC_PROCESSING,))

    async def finish(
        self,
        doc_id: int,
        *,
        status: str,
        stage: str | None = None,
        error: str | None = None,
        metrics: Mapping[str, Any] | None = None,
        page_count: int | None = None,
        chunk_count: int | None = None,
        tokens_used: int | None = None,
        cost_usd: float | None = None,
    ) -> bool:
        """End processing: PUBLISHED (completing ``stage``, default publishing) or FAILED at ``stage``."""
        stages: list[StageChange] = []
        columns: dict[str, Any] = {"status": status, "finished_at": NOW}
        if status == DOC_PUBLISHED:
            stages.append(StageChange(stage or KB_STAGES[-1], STAGE_COMPLETED, metrics=metrics))
            columns.update({"published_at": NOW, "failed_stage": None, "error_message": None})
            for name, value in (
                ("page_count", page_count),
                ("chunk_count", chunk_count),
                ("tokens_used", tokens_used),
                ("cost_usd", cost_usd),
            ):
                if value is not None:
                    columns[name] = value
        elif status == DOC_FAILED:
            failed = stage or KB_STAGES[-1]
            reason = truncate_utf8(error or "Failed", MAX_ERROR_BYTES)
            stages.append(StageChange(failed, STAGE_FAILED, error=reason, metrics=metrics))
            columns.update({"failed_stage": failed, "error_message": reason})
        else:
            raise ValueError(f"finish() takes PUBLISHED or FAILED, not {status!r}")
        return await self._update(doc_id, stages=stages, columns=columns, expect_status=(DOC_PROCESSING,))

    async def touch(self, doc_id: int, worker_id: str | None = None) -> None:
        """Heartbeat: keep ``updated_at`` fresh while a document is being processed."""
        sql = f"UPDATE {T} SET updated_at = :now WHERE id = :id AND status = 'PROCESSING'"
        binds: dict[str, Any] = {"now": utc_now(), "id": int(doc_id)}
        if worker_id:
            sql += " AND worker_id = :worker_id"
            binds["worker_id"] = worker_id[:128]
        await self._db.execute(sql, binds)

    async def recover_stale(self, older_than: datetime) -> list[int]:
        """PROCESSING rows not touched since ``older_than`` become FAILED (interrupted)."""
        rows = await self._db.fetch_all(
            f"""
            SELECT id, upload_status, extraction_status, chunking_status, embedding_status, publishing_status
            FROM {T} WHERE status = 'PROCESSING' AND updated_at < :threshold
            """,
            {"threshold": older_than},
        )
        recovered: list[int] = []
        for row in rows:
            doc_id = int(row["id"])
            if await self._fail_interrupted(doc_id, interrupted_stage(row), stale_before=older_than):
                recovered.append(doc_id)
        if recovered:
            _log.warning("doc_intel: marked interrupted documents as FAILED: %s", recovered)
        return recovered

    async def release_interrupted(self, doc_id: int, worker_id: str) -> bool:
        """Shutdown path: fail the document this worker was processing so it can be retried."""
        row = await self._db.fetch_one(
            f"""
            SELECT id, upload_status, extraction_status, chunking_status, embedding_status, publishing_status
            FROM {T} WHERE id = :id AND status = 'PROCESSING' AND worker_id = :worker_id
            """,
            {"id": int(doc_id), "worker_id": worker_id[:128]},
        )
        if row is None:
            return False
        return await self._fail_interrupted(int(doc_id), interrupted_stage(row), worker_id=worker_id)

    async def find_active_duplicate(self, corpus_id: str, sha256: str) -> int | None:
        row = await self._db.fetch_one(
            f"""
            SELECT id FROM {T}
            WHERE corpus_id = :corpus_id AND sha256 = :sha256 AND status IN ('QUEUED', 'PROCESSING', 'PUBLISHED')
            ORDER BY id FETCH FIRST 1 ROWS ONLY
            """,
            {"corpus_id": corpus_id, "sha256": sha256},
        )
        return int(row["id"]) if row else None

    async def reset_for_retry(self, doc_id: int, from_stage: str) -> bool:
        """FAILED -> QUEUED, with ``from_stage`` and every later stage back to PENDING."""
        start = KB_STAGES.index(from_stage)  # ValueError for an unknown stage
        return await self._update(
            doc_id,
            stages=[StageChange(stage, STAGE_PENDING) for stage in KB_STAGES[start:]],
            columns={"status": DOC_QUEUED, "failed_stage": None, "error_message": None, "finished_at": None},
            expect_status=(DOC_FAILED,),
        )

    async def reset_for_republish(self, doc_id: int) -> bool:
        """PUBLISHED/FAILED -> QUEUED from chunking (the extracted text is reused)."""
        return await self._update(
            doc_id,
            stages=[StageChange(stage, STAGE_PENDING) for stage in ("chunking", "embedding", "publishing")],
            columns={"status": DOC_QUEUED, "failed_stage": None, "error_message": None, "finished_at": None},
            expect_status=(DOC_PUBLISHED, DOC_FAILED),
        )

    async def mark_unpublished(self, doc_id: int) -> bool:
        return await self._update(
            doc_id, stages=[], columns={"status": DOC_UNPUBLISHED}, expect_status=(DOC_PUBLISHED, DOC_FAILED)
        )

    async def update_queue_keys(self, doc_id: int, queue_keys: Sequence[str], *, statuses: Sequence[str]) -> bool:
        return await self._update(
            doc_id, stages=[], columns={"queue_keys": dumps_json(list(queue_keys))}, expect_status=tuple(statuses)
        )

    # ---- monitoring queries -------------------------------------------------------------

    async def status_counts(self) -> dict[str, int]:
        rows = await self._db.fetch_all(f"SELECT status, COUNT(*) AS n FROM {T} GROUP BY status")
        return {str(r["status"]): int(r["n"] or 0) for r in rows}

    async def stuck_documents(self, started_before: datetime) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            f"""
            SELECT id, filename, started_at, updated_at FROM {T}
            WHERE status = 'PROCESSING' AND started_at < :before
            ORDER BY started_at FETCH FIRST 50 ROWS ONLY
            """,
            {"before": started_before},
        )

    async def failed_since(self, since: datetime) -> tuple[int, list[dict[str, Any]]]:
        """(count, newest 20) of documents that failed since ``since``."""
        where = f"FROM {T} WHERE status = 'FAILED' AND COALESCE(finished_at, updated_at) >= :since"
        total = await self._db.fetch_one(f"SELECT COUNT(*) AS n {where}", {"since": since})
        rows = await self._db.fetch_all(
            f"""
            SELECT id, filename, failed_stage, error_message, finished_at {where}
            ORDER BY COALESCE(finished_at, updated_at) DESC FETCH FIRST 20 ROWS ONLY
            """,
            {"since": since},
        )
        return int((total or {}).get("n") or 0), rows

    async def published_documents(self) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            f"SELECT id, filename, corpus_id, vertical, queue_keys, chunk_count FROM {T} WHERE status = 'PUBLISHED' ORDER BY id"
        )

    async def failures(self, days: int) -> list[dict[str, Any]]:
        since = utc_now() - timedelta(days=int(days))
        return await self._db.fetch_all(
            f"""
            SELECT d.id, d.filename, d.failed_stage, d.error_message, d.finished_at, d.updated_at,
                   a.name AS account_name
            FROM {T} d
            LEFT JOIN AIVA_accounts a ON a.id = d.account_id
            WHERE d.status = 'FAILED' AND COALESCE(d.finished_at, d.updated_at) >= :since
            ORDER BY COALESCE(d.finished_at, d.updated_at) DESC, d.id DESC
            FETCH FIRST 500 ROWS ONLY
            """,
            {"since": since},
        )

    async def activity(self, limit: int) -> list[dict[str, Any]]:
        """The most recently changed documents (their latest state), newest first."""
        return await self._db.fetch_all(
            f"""
            SELECT d.id, d.filename, d.status, d.failed_stage, d.error_message, d.chunk_count,
                   d.queue_keys, d.upload_status, d.extraction_status, d.chunking_status,
                   d.embedding_status, d.publishing_status, d.updated_at, a.name AS account_name
            FROM {T} d
            LEFT JOIN AIVA_accounts a ON a.id = d.account_id
            ORDER BY d.updated_at DESC, d.id DESC
            FETCH FIRST :limit ROWS ONLY
            """,
            {"limit": max(1, int(limit))},
        )

    # ---- internals ----------------------------------------------------------------------

    async def _fail_interrupted(
        self,
        doc_id: int,
        stage: str,
        *,
        stale_before: datetime | None = None,
        worker_id: str | None = None,
    ) -> bool:
        return await self._update(
            doc_id,
            stages=[StageChange(stage, STAGE_FAILED, error=INTERRUPTED_REASON)],
            columns={
                "status": DOC_FAILED,
                "failed_stage": stage,
                "error_message": INTERRUPTED_REASON,
                "finished_at": NOW,
            },
            expect_status=(DOC_PROCESSING,),
            stale_before=stale_before,
            worker_id=worker_id,
        )

    async def _update(
        self,
        doc_id: int,
        *,
        stages: Sequence[StageChange],
        columns: Mapping[str, Any],
        expect_status: Sequence[str] | None = None,
        stale_before: datetime | None = None,
        worker_id: str | None = None,
    ) -> bool:
        """Apply stage changes + column values in one transaction (row locked).

        Returns False (and changes nothing) when the row is gone or no longer matches
        ``expect_status`` / ``stale_before`` / ``worker_id``.
        """
        now = utc_now()
        doc_id = int(doc_id)
        async with self._db.connection() as conn:
            current = await self._db.fetch_one(
                f"SELECT status, stage_details, updated_at, worker_id FROM {T} WHERE id = :id FOR UPDATE",
                {"id": doc_id},
                conn=conn,
            )
            if current is None:
                return False
            if expect_status and current.get("status") not in expect_status:
                return False
            if stale_before is not None:
                touched = parse_utc(current.get("updated_at"))
                if touched is None or touched >= stale_before:
                    return False
            if worker_id is not None and current.get("worker_id") != worker_id[:128]:
                return False

            sets: list[str] = []
            binds: dict[str, Any] = {"id": doc_id, "now": now}
            if stages:
                details = loads_json(current.get("stage_details"), {})
                for i, change in enumerate(stages):
                    details = apply_stage_change(details, change, now=now)
                    sets.append(f"{stage_column(change.stage)} = :stage_{i}")
                    binds[f"stage_{i}"] = change.status
                sets.append("stage_details = :stage_details")
                binds["stage_details"] = dumps_json(details)
            for name, value in columns.items():
                if name not in _WRITABLE_COLUMNS:
                    raise ValueError(f"Column not writable: {name!r}")
                if value is NOW:
                    sets.append(f"{name} = :now")
                else:
                    sets.append(f"{name} = :col_{name}")
                    binds[f"col_{name}"] = value
            sets.append("updated_at = :now")
            cur = conn.cursor()
            await cur.execute(f"UPDATE {T} SET {', '.join(sets)} WHERE id = :id", binds)
            return cur.rowcount == 1
