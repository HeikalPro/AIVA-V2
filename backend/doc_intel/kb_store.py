"""The ONLY code in this module that reads or writes the knowledge base (``kb_corpus`` / ``kb_chunk``).

Synchronous (python-oracledb thin, KB pool): call through ``asyncio.to_thread``.
``connection_factory`` is ``EmbeddingService.db.connection`` in production: a context
manager that commits on success and rolls back on error, so each public method below
is exactly one KB transaction.

Publishing is atomic: the document's chunks + vectors and its vertical in the selected
queues' ``queue_groups`` are committed together, under a row lock on the corpus, so the
document becomes searchable for exactly those queues on the next chat message.
Integration tests point ``KbTables`` at the isolated ``DI_TEST_KB_CORPUS`` /
``DI_TEST_KB_CHUNK`` copies, never at the real tables.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

import oracledb

from backend.doc_intel.chunking import PreparedChunk
from backend.doc_intel.constants import KB_CHUNKER_VERSION
from backend.doc_intel.queue_config import (
    UnknownQueueError,
    ensure_queue_groups,
    queues_with_vertical,
    remove_vertical,
    set_vertical_queues,
    to_plain,
)
from backend.services.kb_queue_groups import get_queue_groups_from_config
from embedding_service.db.repo import deterministic_chunk_id

_log = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}$")
# Wait this long for the corpus row lock (another publish / queue change) before failing.
LOCK_WAIT_SECONDS = 30
_INSERT_BATCH = 200
_CORPUS_NOT_FOUND = "Knowledge base corpus not found"
_IN_LIST_BATCH = 500
# python-oracledb turns a longer string bind into a LONG, which VECTOR() refuses (ORA-01461).
_MAX_TEXT_BIND_BYTES = 32767


def vector_text(vector: Sequence[float]) -> str:
    """A vector in Oracle's text format, each component with 9 significant digits.

    Nine digits round-trip any FLOAT32 exactly, so nothing is lost in a VECTOR(..., FLOAT32)
    column, and the text stays short: at most 16 bytes per component (24.6 KB at 1536
    dimensions). ``json.dumps`` prints full float64 precision instead, and a provider that
    returns such values pushes one 1536-dim vector past the 32 KB bind limit.
    """
    return "[" + ",".join(format(float(x), ".9g") for x in vector) + "]"


class PublishFailed(Exception):
    """A knowledge-base write failed; ``reason`` is shown to the admin as-is.

    ``code``: ``corpus_not_found`` | ``unknown_queue`` | ``invalid_config`` | ``internal``
    (a data problem) or ``database`` | ``locked`` (the KB database; worth retrying).
    """

    def __init__(self, reason: str, *, code: str = "internal") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code

    @property
    def is_database_error(self) -> bool:
        return self.code in ("database", "locked")


@dataclass(frozen=True)
class KbTables:
    corpus: str = "kb_corpus"
    chunk: str = "kb_chunk"

    def __post_init__(self) -> None:
        for name in (self.corpus, self.chunk):
            if not isinstance(name, str) or not _IDENTIFIER.match(name):
                raise ValueError(f"Invalid table name: {name!r}")


class KbStore:
    def __init__(
        self,
        connection_factory: Callable[[], AbstractContextManager[Any]],
        tables: KbTables | None = None,
    ) -> None:
        self._connect = connection_factory
        self.tables = tables or KbTables()

    @property
    def connection_factory(self) -> Callable[[], AbstractContextManager[Any]]:
        return self._connect

    # ---- reads ------------------------------------------------------------------------------

    def get_corpus_config(self, corpus_id_hex: str) -> dict[str, Any] | None:
        """The corpus ``config_json`` as a plain dict (Decimals converted), or None if absent."""
        cid = _corpus_bytes(corpus_id_hex)
        if cid is None:
            return None
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT config_json FROM {self.tables.corpus} WHERE corpus_id = :cid", {"cid": cid})
            row = cur.fetchone()
        if row is None:
            return None
        return _config_dict(row[0])

    def chunk_counts(self, corpus_id_hex: str, parent_ids: Sequence[str]) -> dict[str, int]:
        """Chunk count per ``external_parent_id`` (0 when a parent has none)."""
        ids = list(dict.fromkeys(str(p) for p in parent_ids if p))
        counts = {p: 0 for p in ids}
        cid = _corpus_bytes(corpus_id_hex)
        if not ids or cid is None:
            return counts
        with self._connect() as conn, conn.cursor() as cur:
            for start in range(0, len(ids), _IN_LIST_BATCH):
                part = ids[start : start + _IN_LIST_BATCH]
                binds: dict[str, Any] = {f"p{i}": pid for i, pid in enumerate(part)}
                binds["cid"] = cid
                placeholders = ", ".join(f":p{i}" for i in range(len(part)))
                cur.execute(
                    f"""
                    SELECT external_parent_id, COUNT(*)
                    FROM {self.tables.chunk}
                    WHERE corpus_id = :cid AND external_parent_id IN ({placeholders})
                    GROUP BY external_parent_id
                    """,
                    binds,
                )
                for pid, n in cur.fetchall():
                    counts[str(pid)] = int(n)
        return counts

    def ping(self) -> int:
        """Round trip ``SELECT 1`` on the KB pool; latency in milliseconds."""
        start = time.perf_counter()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM dual")
            cur.fetchone()
        return int(round((time.perf_counter() - start) * 1000))

    # ---- writes -----------------------------------------------------------------------------

    def publish(
        self,
        *,
        corpus_id_hex: str,
        vertical: str,
        queue_keys: list[str],
        chunks: list[PreparedChunk],
        vectors: list[list[float]],
        embedding_model: str,
        dimension: int,
    ) -> dict[str, Any]:
        """Replace this document's chunks and put its vertical in exactly ``queue_keys``, atomically."""
        if len(chunks) != len(vectors):
            raise PublishFailed(f"Internal error: {len(chunks)} chunks but {len(vectors)} vectors", code="internal")
        dim = int(dimension)
        if dim < 1 or dim > 65535:
            raise PublishFailed(f"Invalid vector dimension: {dimension}", code="invalid_config")
        cid = _corpus_bytes(corpus_id_hex)
        if cid is None:
            raise PublishFailed(_CORPUS_NOT_FOUND, code="corpus_not_found")

        insert_sql = f"""
            INSERT INTO {self.tables.chunk} (
                chunk_id, corpus_id, external_parent_id, chunk_index, chunker_version,
                content_hash, chunk_text, payload_json, embedding, embedding_model,
                embedding_version, created_at, updated_at
            ) VALUES (
                :chunk_id, :cid, :pid, :chunk_index, :chunker_version,
                :content_hash, :chunk_text, CAST(:payload AS JSON), VECTOR(:emb, {dim}, FLOAT32), :model,
                '1', SYSTIMESTAMP, SYSTIMESTAMP
            )
        """
        rows = [
            {
                "chunk_id": deterministic_chunk_id(cid, vertical, chunk.index, KB_CHUNKER_VERSION),
                "cid": cid,
                "pid": vertical,
                "chunk_index": chunk.index,
                "chunker_version": KB_CHUNKER_VERSION,
                "content_hash": chunk.content_hash,
                "chunk_text": chunk.text,
                "payload": json.dumps(to_plain(chunk.payload), ensure_ascii=False),
                "emb": vector_text(vec),
                "model": embedding_model,
            }
            for chunk, vec in zip(chunks, vectors)
        ]
        if any(len(row["emb"]) > _MAX_TEXT_BIND_BYTES for row in rows):  # ASCII: characters == bytes
            raise PublishFailed(
                f"Vectors of {dim} dimensions are too large to write as text (at most about 2,000 dimensions)",
                code="invalid_config",
            )
        try:
            with self._connect() as conn, conn.cursor() as cur:
                before = self._lock_config(cur, cid)
                if before is None:
                    raise PublishFailed(_CORPUS_NOT_FOUND, code="corpus_not_found")
                after = _with_queues(before, vertical, queue_keys)
                cur.execute(
                    f"DELETE FROM {self.tables.chunk} WHERE corpus_id = :cid AND external_parent_id = :pid",
                    {"cid": cid, "pid": vertical},
                )
                deleted = int(cur.rowcount or 0)
                for start in range(0, len(rows), _INSERT_BATCH):
                    cur.executemany(insert_sql, rows[start : start + _INSERT_BATCH])
                self._write_config(cur, cid, after)
        except PublishFailed:
            raise
        except oracledb.DatabaseError as ex:
            raise _db_failure(ex) from ex
        summary = {
            "chunks_written": len(rows),
            "chunks_replaced": deleted,
            **_queue_changes(before, after, queue_keys),
        }
        _log.info(
            "doc_intel: published %s (%s chunks) to %s", vertical, len(rows), ", ".join(queue_keys)
        )
        return summary

    def unpublish(self, *, corpus_id_hex: str, vertical: str) -> dict[str, Any]:
        """Remove ``vertical`` from every queue first, then delete its chunks (one transaction).

        Idempotent: unpublishing something that is not published changes nothing.
        """
        cid = _corpus_bytes(corpus_id_hex)
        if cid is None:
            return {"corpus_found": False, "removed_from_queues": [], "chunks_deleted": 0}
        try:
            with self._connect() as conn, conn.cursor() as cur:
                before = self._lock_config(cur, cid)
                removed_from: list[str] = []
                if before is not None:
                    removed_from = queues_with_vertical(before, vertical)
                    after = remove_vertical(before, vertical)
                    if after != before:
                        self._write_config(cur, cid, after)
                cur.execute(
                    f"DELETE FROM {self.tables.chunk} WHERE corpus_id = :cid AND external_parent_id = :pid",
                    {"cid": cid, "pid": vertical},
                )
                deleted = int(cur.rowcount or 0)
        except oracledb.DatabaseError as ex:
            raise _db_failure(ex) from ex
        _log.info("doc_intel: unpublished %s (%s chunks, queues %s)", vertical, deleted, removed_from)
        return {"corpus_found": before is not None, "removed_from_queues": removed_from, "chunks_deleted": deleted}

    def set_queues(self, *, corpus_id_hex: str, vertical: str, queue_keys: list[str]) -> dict[str, Any]:
        """Put ``vertical`` in exactly ``queue_keys`` (config only; chunks are untouched)."""
        cid = _corpus_bytes(corpus_id_hex)
        if cid is None:
            raise PublishFailed(_CORPUS_NOT_FOUND, code="corpus_not_found")
        try:
            with self._connect() as conn, conn.cursor() as cur:
                before = self._lock_config(cur, cid)
                if before is None:
                    raise PublishFailed(_CORPUS_NOT_FOUND, code="corpus_not_found")
                after = _with_queues(before, vertical, queue_keys)
                if after != before:
                    self._write_config(cur, cid, after)
        except PublishFailed:
            raise
        except oracledb.DatabaseError as ex:
            raise _db_failure(ex) from ex
        return _queue_changes(before, after, queue_keys)

    # ---- helpers ----------------------------------------------------------------------------

    def _lock_config(self, cur: Any, cid: bytes) -> dict[str, Any] | None:
        cur.execute(
            f"SELECT config_json FROM {self.tables.corpus} WHERE corpus_id = :cid FOR UPDATE WAIT {LOCK_WAIT_SECONDS}",
            {"cid": cid},
        )
        row = cur.fetchone()
        return None if row is None else _config_dict(row[0])

    def _write_config(self, cur: Any, cid: bytes, config: dict[str, Any]) -> None:
        cur.execute(
            f"UPDATE {self.tables.corpus} SET config_json = CAST(:cfg AS JSON), updated_at = SYSTIMESTAMP WHERE corpus_id = :cid",
            {"cfg": json.dumps(to_plain(config), ensure_ascii=False), "cid": cid},
        )


def _corpus_bytes(corpus_id_hex: str | None) -> bytes | None:
    try:
        raw = bytes.fromhex(str(corpus_id_hex or "").replace("-", "").strip())
    except ValueError:
        return None
    return raw or None


def _config_dict(raw: Any) -> dict[str, Any]:
    """Native JSON column value (dict with Decimals), JSON text or a LOB -> plain dict."""
    value = raw
    if hasattr(value, "read"):
        value = value.read()
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        value = json.loads(value) if value.strip() else {}
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PublishFailed("Knowledge base configuration is not a JSON object", code="invalid_config")
    return to_plain(value)


def _with_queues(config: dict[str, Any], vertical: str, queue_keys: list[str]) -> dict[str, Any]:
    try:
        return set_vertical_queues(config, vertical, queue_keys)
    except UnknownQueueError as ex:
        raise PublishFailed(f"Queue '{ex.key}' no longer exists in this knowledge base", code="unknown_queue") from None


def _queue_changes(before: dict[str, Any], after: dict[str, Any], selected: list[str]) -> dict[str, Any]:
    """Verticals before/after of the selected and otherwise changed queues (for stage details)."""
    old = get_queue_groups_from_config(before)
    new = get_queue_groups_from_config(after)
    raw = before.get("queue_groups")
    materialized = not isinstance(raw, dict) or ensure_queue_groups(before)["queue_groups"] != raw
    changed = {k for k in set(old) | set(new) if old.get(k, {}).get("verticals") != new.get(k, {}).get("verticals")}
    keys = sorted(changed | {str(k).strip() for k in selected})
    return {
        "materialized_default_queue_groups": bool(materialized),
        "queues": {
            k: {"before": list(old.get(k, {}).get("verticals", [])), "after": list(new.get(k, {}).get("verticals", []))}
            for k in keys
        },
    }


_LOCKED = "The knowledge base configuration is locked by another operation — try again in a minute"
_FRIENDLY_ORA = {
    "ORA-30006": (_LOCKED, "locked"),
    "ORA-00054": (_LOCKED, "locked"),
    "ORA-00257": ("Knowledge base database error: the database host disk is full (ORA-00257 archiver error)", "database"),
}


def _db_failure(ex: BaseException) -> PublishFailed:
    """``PublishFailed`` for an oracledb error: its first line, never the connect string."""
    first = str(ex).strip().splitlines()[0] if str(ex).strip() else type(ex).__name__
    for ora, (text, code) in _FRIENDLY_ORA.items():
        if ora in first:
            return PublishFailed(text, code=code)
    return PublishFailed(f"Knowledge base database error: {first[:300]}", code="database")
