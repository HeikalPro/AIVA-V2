from __future__ import annotations

import json
import logging
from typing import Any

from backend.database import Database
from backend.utils import serialize_row

_log = logging.getLogger(__name__)

_CHUNK_PREVIEW_CHARS = 300
_MAX_CHUNKS_STORED = 20


def _compact_chunks(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep a small, JSON-safe trace of each retrieved chunk (no full text)."""
    out: list[dict[str, Any]] = []
    for ch in chunks[:_MAX_CHUNKS_STORED]:
        text = ch.get("text") or ch.get("content") or ""
        preview = str(text)[:_CHUNK_PREVIEW_CHARS]
        score = ch.get("score")
        out.append(
            {
                "parent_id": str(ch.get("parent_id") or "").strip().lstrip("/") or None,
                "chunk_index": ch.get("chunk_index"),
                "score": float(score) if isinstance(score, (int, float)) else None,
                "text_preview": preview,
            }
        )
    return out


def _top_score(chunks: list[dict[str, Any]]) -> float | None:
    scores = [ch.get("score") for ch in chunks if isinstance(ch.get("score"), (int, float))]
    return float(max(scores)) if scores else None


async def persist_rag_retrieval(
    db: Database | None,
    *,
    session_id: int | None,
    account_id: int | None,
    corpus_id: str | None,
    query_text: str | None,
    top_k: int | None,
    verticals: list[str] | None,
    status: str,
    chunks: list[dict[str, Any]] | None,
    retrieval_ms: int | None,
    error_message: str | None,
    source: str | None = None,
) -> None:
    """Best-effort write of a RAG retrieval trace; never raises to the caller."""
    if db is None:
        return
    chunk_list = chunks or []
    try:
        chunks_json = json.dumps(_compact_chunks(chunk_list)) if chunk_list else None
        verticals_str = ",".join(v for v in (verticals or []) if v)[:512] or None
        await db.execute(
            """
            INSERT INTO AIVA_rag_retrievals (
                session_id, account_id, corpus_id, query_text, top_k, verticals,
                status, chunks_returned, top_score, retrieval_ms, error_message,
                chunks_json, source
            ) VALUES (
                :session_id, :account_id, :corpus_id, :query_text, :top_k, :verticals,
                :status, :chunks_returned, :top_score, :retrieval_ms, :error_message,
                :chunks_json, :source
            )
            """,
            {
                "session_id": session_id,
                "account_id": account_id,
                "corpus_id": (corpus_id or "")[:128] or None,
                "query_text": query_text or None,
                "top_k": top_k,
                "verticals": verticals_str,
                "status": (status or "UNKNOWN")[:16],
                "chunks_returned": len(chunk_list),
                "top_score": _top_score(chunk_list),
                "retrieval_ms": retrieval_ms,
                "error_message": (error_message or "")[:2000] or None,
                "chunks_json": chunks_json,
                "source": (source or "")[:16] or None,
            },
        )
    except Exception:
        _log.warning("Failed to persist RAG retrieval log", exc_info=True)


async def list_rag_retrievals(
    db: Database,
    *,
    org_id: int | None = None,
    account_id: int | None = None,
    session_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    binds: dict[str, Any] = {"limit": limit, "offset": offset}
    filters = ["1=1"]
    if org_id is not None:
        filters.append("a.organization_id = :org_id")
        binds["org_id"] = org_id
    if account_id is not None:
        filters.append("rr.account_id = :account_id")
        binds["account_id"] = account_id
    if session_id is not None:
        filters.append("rr.session_id = :session_id")
        binds["session_id"] = session_id
    if status:
        filters.append("rr.status = :status")
        binds["status"] = status.upper()

    where_sql = " AND ".join(filters)
    rows = await db.fetch_all(
        f"""
        SELECT rr.id, rr.created_at, rr.session_id, rr.account_id, rr.corpus_id,
               rr.query_text, rr.top_k, rr.verticals, rr.status, rr.chunks_returned,
               rr.top_score, rr.retrieval_ms, rr.error_message, rr.chunks_json,
               rr.source, a.name AS account_name
        FROM AIVA_rag_retrievals rr
        LEFT JOIN AIVA_accounts a ON a.id = rr.account_id
        WHERE {where_sql}
        ORDER BY rr.created_at DESC, rr.id DESC
        OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """,
        binds,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        data = serialize_row(r) or {}
        data["summary"] = _rag_summary(data)
        out.append(data)
    return out


def _rag_summary(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "?")
    count = row.get("chunks_returned")
    top = row.get("top_score")
    acct = row.get("account_name") or (f"account #{row['account_id']}" if row.get("account_id") else "unknown account")
    top_str = f", top score {float(top):.3f}" if isinstance(top, (int, float)) else ""
    return f"{status} — {count} chunks{top_str} — {acct}"
