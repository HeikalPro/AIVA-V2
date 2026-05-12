from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Sequence

from embedding_service.adapters.base import ChunkDraft
from embedding_service.util.uuids import bytes_to_hex, new_uuid_bytes

_log = logging.getLogger(__name__)

# Deterministic UUIDv5 namespace for chunk primary keys
_CHUNK_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def deterministic_chunk_id(
    corpus_id: bytes,
    parent_id: str,
    chunk_index: int,
    chunker_version: str,
) -> bytes:
    key = f"{corpus_id.hex()}|{parent_id}|{chunk_index}|{chunker_version}"
    return uuid.uuid5(_CHUNK_NS, key).bytes


@dataclass
class CorpusRow:
    corpus_id: bytes
    name: str
    slug: str
    config: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


def insert_corpus(conn, name: str, slug: str, config: dict[str, Any]) -> bytes:
    cid = new_uuid_bytes()
    cfg_json = json.dumps(config, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb_corpus (corpus_id, name, slug, config_json, created_at, updated_at)
            VALUES (:cid, :name, :slug, CAST(:cfg AS JSON), SYSTIMESTAMP, SYSTIMESTAMP)
            """,
            cid=cid,
            name=name,
            slug=slug,
            cfg=cfg_json,
        )
    return cid


def get_corpus_by_id(conn, corpus_id: bytes) -> CorpusRow | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT corpus_id, name, slug, config_json, created_at, updated_at
            FROM kb_corpus WHERE corpus_id = :cid
            """,
            cid=corpus_id,
        )
        row = cur.fetchone()
    if not row:
        return None
    cfg = row[3]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    elif hasattr(cfg, "read"):
        cfg = json.loads(cfg.read())
    return CorpusRow(
        corpus_id=row[0],
        name=row[1],
        slug=row[2],
        config=cfg,
        created_at=str(row[4]) if row[4] is not None else None,
        updated_at=str(row[5]) if row[5] is not None else None,
    )


def list_corpora(conn) -> list[CorpusRow]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT corpus_id, name, slug, config_json, created_at, updated_at
            FROM kb_corpus ORDER BY created_at DESC
            """
        )
        rows = cur.fetchall()
    out: list[CorpusRow] = []
    for row in rows:
        cfg = row[3]
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        elif hasattr(cfg, "read"):
            cfg = json.loads(cfg.read())
        out.append(
            CorpusRow(
                corpus_id=row[0],
                name=row[1],
                slug=row[2],
                config=cfg,
                created_at=str(row[4]) if row[4] is not None else None,
                updated_at=str(row[5]) if row[5] is not None else None,
            )
        )
    return out


def get_corpus_by_slug(conn, slug: str) -> CorpusRow | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT corpus_id, name, slug, config_json, created_at, updated_at
            FROM kb_corpus WHERE slug = :slug
            """,
            slug=slug,
        )
        row = cur.fetchone()
    if not row:
        return None
    cfg = row[3]
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    elif hasattr(cfg, "read"):
        cfg = json.loads(cfg.read())
    return CorpusRow(
        corpus_id=row[0],
        name=row[1],
        slug=row[2],
        config=cfg,
        created_at=str(row[4]) if row[4] is not None else None,
        updated_at=str(row[5]) if row[5] is not None else None,
    )


def update_corpus(conn, corpus_id: bytes, *, name: str | None, config: dict[str, Any] | None) -> None:
    parts = []
    binds: dict[str, Any] = {"cid": corpus_id}
    if name is not None:
        parts.append("name = :name")
        binds["name"] = name
    if config is not None:
        parts.append("config_json = CAST(:cfg AS JSON)")
        binds["cfg"] = json.dumps(config, ensure_ascii=False)
    if not parts:
        return
    parts.append("updated_at = SYSTIMESTAMP")
    sql = f"UPDATE kb_corpus SET {', '.join(parts)} WHERE corpus_id = :cid"
    with conn.cursor() as cur:
        cur.execute(sql, binds)


def merge_chunk_row(
    conn,
    *,
    corpus_id: bytes,
    chunker_version: str,
    draft: ChunkDraft,
) -> bytes:
    chunk_id = deterministic_chunk_id(
        corpus_id, draft.external_parent_id, draft.chunk_index, chunker_version
    )
    payload_s = json.dumps(draft.payload, ensure_ascii=False)
    with conn.cursor() as cur:
        cur.execute(
            """
            MERGE INTO kb_chunk c
            USING (
                SELECT :chunk_id AS chunk_id,
                       :corpus_id AS corpus_id,
                       :external_parent_id AS external_parent_id,
                       :chunk_index AS chunk_index,
                       :chunker_version AS chunker_version,
                       :content_hash AS content_hash,
                       :chunk_text AS chunk_text,
                       CAST(:payload_json AS JSON) AS payload_json,
                       :source_line AS source_line
                FROM DUAL
            ) s
            ON (
                c.corpus_id = s.corpus_id
                AND c.external_parent_id = s.external_parent_id
                AND c.chunk_index = s.chunk_index
                AND c.chunker_version = s.chunker_version
            )
            WHEN MATCHED THEN UPDATE SET
                c.chunk_text = s.chunk_text,
                c.payload_json = s.payload_json,
                c.source_line = s.source_line,
                c.updated_at = SYSTIMESTAMP,
                c.embedding = CASE WHEN c.content_hash = s.content_hash THEN c.embedding ELSE NULL END,
                c.embedding_model = CASE WHEN c.content_hash = s.content_hash THEN c.embedding_model ELSE NULL END,
                c.embedding_version = CASE WHEN c.content_hash = s.content_hash THEN c.embedding_version ELSE NULL END,
                c.content_hash = s.content_hash
            WHEN NOT MATCHED THEN INSERT (
                chunk_id, corpus_id, external_parent_id, chunk_index, chunker_version,
                content_hash, chunk_text, payload_json, embedding, embedding_model,
                embedding_version, source_uri, source_line, created_at, updated_at
            ) VALUES (
                s.chunk_id, s.corpus_id, s.external_parent_id, s.chunk_index, s.chunker_version,
                s.content_hash, s.chunk_text, s.payload_json, NULL, NULL,
                NULL, NULL, s.source_line, SYSTIMESTAMP, SYSTIMESTAMP
            )
            """,
            chunk_id=chunk_id,
            corpus_id=corpus_id,
            external_parent_id=draft.external_parent_id,
            chunk_index=draft.chunk_index,
            chunker_version=chunker_version,
            content_hash=draft.content_hash,
            chunk_text=draft.text,
            payload_json=payload_s,
            source_line=draft.source_line,
        )
    return chunk_id


def fetch_chunks_missing_embedding(
    conn, corpus_id: bytes, limit: int
) -> list[tuple[bytes, str]]:
    """Return (chunk_id, chunk_text) rows with NULL embedding."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT chunk_id, chunk_text
            FROM kb_chunk
            WHERE corpus_id = :cid AND embedding IS NULL
            FETCH FIRST :lim ROWS ONLY
            """,
            cid=corpus_id,
            lim=limit,
        )
        out: list[tuple[bytes, str]] = []
        for r in cur:
            raw_text = r[1]
            if raw_text is not None and hasattr(raw_text, "read"):
                raw_text = raw_text.read()
            out.append((r[0], raw_text or ""))
        return out


def count_chunks_missing_embedding(conn, corpus_id: bytes) -> int:
    """Rows in kb_chunk for this corpus with NULL embedding (for progress totals)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM kb_chunk
            WHERE corpus_id = :cid AND embedding IS NULL
            """,
            cid=corpus_id,
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0


def update_chunk_embedding(
    conn,
    *,
    chunk_id: bytes,
    vector: Sequence[float],
    model: str,
    version: str,
    dimension: int,
) -> None:
    # Bind as JSON text and construct VECTOR in SQL for broad driver compatibility.
    vec_json = json.dumps(list(vector), ensure_ascii=False)
    dim_lit = int(dimension)
    if dim_lit < 1 or dim_lit > 65535:
        raise ValueError(f"Invalid vector dimension: {dimension}")
    sql = f"""
            UPDATE kb_chunk
            SET embedding = VECTOR(:emb_json, {dim_lit}, FLOAT32),
                embedding_model = :model,
                embedding_version = :ever,
                updated_at = SYSTIMESTAMP
            WHERE chunk_id = :cid
            """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            emb_json=vec_json,
            model=model,
            ever=version,
            cid=chunk_id,
        )


def delete_chunks_for_corpus(conn, corpus_id: bytes) -> int:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM kb_chunk WHERE corpus_id = :cid", cid=corpus_id)
        return cur.rowcount or 0


def insert_job(conn, job_id: bytes, corpus_id: bytes, job_type: str, status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kb_ingest_job (job_id, corpus_id, job_type, status, stats_json, created_at, updated_at)
            VALUES (:jid, :cid, :jtype, :st, CAST(:stats AS JSON), SYSTIMESTAMP, SYSTIMESTAMP)
            """,
            jid=job_id,
            cid=corpus_id,
            jtype=job_type,
            st=status,
            stats="{}",
        )


def update_job(
    conn,
    job_id: bytes,
    *,
    status: str | None = None,
    stats: dict[str, Any] | None = None,
    error_msg: str | None = None,
) -> None:
    parts = ["updated_at = SYSTIMESTAMP"]
    binds: dict[str, Any] = {"jid": job_id}
    if status is not None:
        parts.append("status = :st")
        binds["st"] = status
    if stats is not None:
        parts.append("stats_json = CAST(:stats AS JSON)")
        binds["stats"] = json.dumps(stats, ensure_ascii=False)
    if error_msg is not None:
        parts.append("error_msg = :err")
        binds["err"] = error_msg[:4000]
    sql = f"UPDATE kb_ingest_job SET {', '.join(parts)} WHERE job_id = :jid"
    with conn.cursor() as cur:
        cur.execute(sql, binds)


def get_job(conn, job_id: bytes) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, corpus_id, job_type, status, stats_json, error_msg, created_at, updated_at
            FROM kb_ingest_job WHERE job_id = :jid
            """,
            jid=job_id,
        )
        row = cur.fetchone()
    if not row:
        return None
    stats = row[4]
    if stats is None:
        stats_dict: Any = {}
    elif isinstance(stats, str):
        stats_dict = json.loads(stats)
    elif hasattr(stats, "read"):
        stats_dict = json.loads(stats.read())
    else:
        try:
            stats_dict = dict(stats)  # type: ignore[arg-type]
        except Exception:
            stats_dict = {}
    return {
        "job_id": bytes_to_hex(row[0]),
        "corpus_id": bytes_to_hex(row[1]),
        "job_type": row[2],
        "status": row[3],
        "stats": stats_dict,
        "error_msg": row[5],
        "created_at": str(row[6]) if row[6] else None,
        "updated_at": str(row[7]) if row[7] else None,
    }


_VECTOR_METRIC_SQL = {
    "COSINE": "COSINE",
    "EUCLIDEAN": "EUCLIDEAN",
    "DOT": "DOT",
}


def search_similar(
    conn,
    *,
    corpus_id: bytes,
    query_vector: Sequence[float],
    top_k: int,
    metric: str,
    vertical: str | None,
    interaction_type: str | None,
    issue_type: str | None,
    escalation: str | None,
) -> list[dict[str, Any]]:
    metric_sql = _VECTOR_METRIC_SQL.get(metric.upper(), "COSINE")
    qv = list(query_vector)
    dim_lit = len(qv)
    if dim_lit < 1 or dim_lit > 65535:
        raise ValueError(f"Invalid query vector dimension: {dim_lit}")
    qjson = json.dumps(qv, ensure_ascii=False)

    filters = ["c.corpus_id = :cid", "c.embedding IS NOT NULL"]
    binds: dict[str, Any] = {"cid": corpus_id, "qjson": qjson, "k": top_k}
    if vertical is not None:
        filters.append(
            "JSON_VALUE(c.payload_json, '$.vertical' RETURNING VARCHAR2(256)) = :vertical"
        )
        binds["vertical"] = vertical
    if interaction_type is not None:
        filters.append(
            "JSON_VALUE(c.payload_json, '$.interaction_type' RETURNING VARCHAR2(256)) = :itype"
        )
        binds["itype"] = interaction_type
    if issue_type is not None:
        filters.append(
            "JSON_VALUE(c.payload_json, '$.issue_type' RETURNING VARCHAR2(256)) = :issue"
        )
        binds["issue"] = issue_type
    if escalation is not None:
        filters.append(
            "JSON_VALUE(c.payload_json, '$.escalation' RETURNING VARCHAR2(32)) = :esc"
        )
        binds["esc"] = escalation

    where_sql = " AND ".join(filters)
    sql = f"""
        SELECT c.chunk_id, c.external_parent_id, c.chunk_index, c.chunk_text, c.payload_json,
               VECTOR_DISTANCE(c.embedding, VECTOR(:qjson, {dim_lit}, FLOAT32), {metric_sql}) AS dist
        FROM kb_chunk c
        WHERE {where_sql}
        ORDER BY dist
        FETCH FIRST :k ROWS ONLY
    """
    out: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        for row in cur:
            dist = float(row[5])
            sim = 1.0 / (1.0 + dist)
            payload = row[4]
            if isinstance(payload, str):
                pdict = json.loads(payload)
            elif hasattr(payload, "read"):
                pdict = json.loads(payload.read())
            else:
                pdict = dict(payload) if payload else {}
            raw_text = row[3]
            if raw_text is not None and hasattr(raw_text, "read"):
                raw_text = raw_text.read()
            out.append(
                {
                    "chunk_id": bytes_to_hex(row[0]),
                    "parent_id": row[1],
                    "chunk_index": int(row[2]),
                    "text": raw_text or "",
                    "payload": pdict,
                    "score": sim,
                }
            )
    return out