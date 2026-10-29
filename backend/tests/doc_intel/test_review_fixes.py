"""Regression tests for defects found in the Agent 5 review (see the review report).

- The traceback written to AIVA_error_logs was not scrubbed, although the message was.
- An upload whose duplicate lookup failed, or that was cancelled, left the client's file
  in the document store with no registry row pointing at it.
- Oracle integration run: a vector whose JSON text passed 32 KB failed to publish
  (ORA-01461), and health details over 32 KB failed to store (ORA-03146 in the MERGE).
"""
from __future__ import annotations

import asyncio
import io
import json
import random
import struct
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import UploadFile

from backend.doc_intel import storage as real_storage
from backend.doc_intel.health import CheckResult
from backend.doc_intel.health_repo import HealthRepo
from backend.doc_intel.kb_import import KbImportService
from backend.doc_intel.kb_store import KbStore, KbTables, PublishFailed, vector_text

from .conftest import (
    ACCOUNT_ID,
    CORPUS_ID,
    PDF_BYTES,
    Env,
    RecordingDatabase,
    RecordingKbConnection,
    corpus_config,
    make_user,
)

SA = make_user("SUPER_ADMIN")
SECRET = "sk-live-4f9c2b7d1e8a6c3b"
CAUSE_SECRET = "sk-cause-7a1d9e3c5b2f"


# ---- error log hygiene ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_log_traceback_is_scrubbed_like_the_message(env: Env):
    env.settings.error_log_enabled = True
    error = RuntimeError(f"provider rejected Authorization: Bearer {SECRET}")
    error.__cause__ = ValueError(f"upstream echoed key {CAUSE_SECRET}")
    env.extractor.error = error
    out = await env.upload(("leaky.pdf", PDF_BYTES + b"% leaky\n"))
    doc_id = out.documents[0].id
    assert await env.process_next() == "FAILED"

    (logged,) = env.errors
    for field in ("exception_message", "stack_trace"):
        assert SECRET not in logged[field], field
        assert CAUSE_SECRET not in logged[field], field
    assert "[redacted]" in logged["stack_trace"]
    assert "RuntimeError" in logged["stack_trace"] and "ValueError" in logged["stack_trace"]  # still useful
    assert SECRET not in (env.row(doc_id)["error_message"] or "")


# ---- no orphaned files in the document store -----------------------------------------------


class _Storage:
    """The real storage module, with one function swapped out."""

    def __init__(self, **overrides: Any) -> None:
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(real_storage, name)


def _service(env: Env, *, storage: Any = real_storage, repo: Any = None) -> KbImportService:
    return KbImportService(
        db=None,
        repo=repo or env.repo,
        kb=env.kb,
        settings=env.settings,
        embedder_factory=lambda cfg: env.embedder,
        extract_fn=env.extractor,
        storage=storage,
    )


def _kb_root(env: Env) -> Path:
    return env.settings.storage_path / "kb"


def _leftovers(env: Env) -> list[Path]:
    root = _kb_root(env)
    return sorted(root.iterdir()) if root.exists() else []


def _upload(name: str = "Card FAQ.pdf", data: bytes = PDF_BYTES) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


class _BrokenLookupRepo:
    """Delegates to the fake registry, but the duplicate lookup fails (app DB down)."""

    def __init__(self, inner: Any, error: BaseException) -> None:
        self._inner = inner
        self._error = error

    async def find_active_duplicate(self, corpus_id: str, sha256: str) -> int | None:
        raise self._error

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("DPY-6005: cannot connect to database"), asyncio.CancelledError()])
async def test_failed_duplicate_lookup_leaves_no_file_behind(env: Env, error: BaseException):
    service = _service(env, repo=_BrokenLookupRepo(env.repo, error))
    with pytest.raises(type(error)):
        await service.upload(SA, ACCOUNT_ID, ["HALAN"], [_upload()])
    assert _leftovers(env) == []
    assert env.repo.rows == {}


@pytest.mark.asyncio
async def test_cancelled_upload_leaves_no_directory_behind(env: Env):
    async def cancelled_save(upload: Any, doc_dir: Path, *, settings: Any) -> Any:
        raise asyncio.CancelledError()

    service = _service(env, storage=_Storage(save_upload=cancelled_save))
    with pytest.raises(asyncio.CancelledError):
        await service.upload(SA, ACCOUNT_ID, ["HALAN"], [_upload()])
    assert _leftovers(env) == []
    assert env.repo.rows == {}


class _CancelledInsertRepo(_BrokenLookupRepo):
    async def find_active_duplicate(self, corpus_id: str, sha256: str) -> int | None:
        return None

    async def insert_document(self, **kw: Any) -> int:
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_cancelled_registry_insert_leaves_no_file_behind(env: Env):
    service = _service(env, repo=_CancelledInsertRepo(env.repo, RuntimeError("unused")))
    with pytest.raises(asyncio.CancelledError):
        await service.upload(SA, ACCOUNT_ID, ["HALAN"], [_upload()])
    assert _leftovers(env) == []


@pytest.mark.asyncio
async def test_successful_upload_still_keeps_its_file(env: Env):
    service = _service(env)
    out = await service.upload(SA, ACCOUNT_ID, ["HALAN"], [_upload()])
    assert out.accepted == 1
    (kept,) = _leftovers(env)
    assert (kept / "original.pdf").is_file()
    assert Path(env.row(out.documents[0].id)["storage_dir"]) == kept


# ---- vectors are written as compact text (ORA-01461 above 32 KB) --------------------------------

_WORST_CASE = -1.2345678901234567e-05  # sign, 9 significant digits and an exponent: 15 characters


def _f32(x: float) -> float:
    return struct.unpack("f", struct.pack("f", x))[0]


def test_vector_text_round_trips_float32_exactly():
    rnd = random.Random(7)
    values = [_f32(rnd.uniform(-1, 1) * 10 ** -rnd.randint(0, 8)) for _ in range(5000)]
    values += [_f32(x) for x in (0.0, -0.0, 1.0, -1.0, 1e-30, 3.4e38, 0.1)]
    parsed = json.loads(vector_text(values))  # valid JSON, and Oracle's vector text format
    assert [_f32(p) for p in parsed] == values


def test_a_1536_dim_vector_stays_far_below_the_32k_bind_limit():
    full_precision = [_WORST_CASE] * 1536
    assert len(json.dumps(full_precision, separators=(",", ":"))) > 32767  # what used to be sent
    assert len(vector_text(full_precision)) <= 1536 * 16 + 1 < 32767


def _store() -> tuple[KbStore, RecordingKbConnection]:
    conn = RecordingKbConnection()
    conn.on(r"SELECT config_json", [(corpus_config(),)])
    return KbStore(conn, KbTables("DI_TEST_KB_CORPUS", "DI_TEST_KB_CHUNK")), conn


def _chunks(n: int):
    from backend.doc_intel.chunking import PreparedChunk

    return [
        PreparedChunk(index=i, text=f"chunk {i}", content_hash="0" * 64, pages=(1,), section=None,
                      payload={"vertical": "kbdoc-7", "chunk_index": i})
        for i in range(n)
    ]


def test_publish_binds_the_compact_vector_text():
    kb, conn = _store()
    vectors = [[_WORST_CASE] * 1536, [0.25] * 1536]
    kb.publish(corpus_id_hex=CORPUS_ID, vertical="kbdoc-7", queue_keys=["HALAN"], chunks=_chunks(2),
               vectors=vectors, embedding_model="m", dimension=1536)
    (_, rows), = [(s, p) for s, p in conn.statements if s.startswith("INSERT INTO DI_TEST_KB_CHUNK")]
    assert [row["emb"] for row in rows] == [vector_text(v) for v in vectors]
    assert max(len(row["emb"]) for row in rows) < 32767
    assert json.loads(rows[1]["emb"]) == [0.25] * 1536


def test_publish_refuses_vectors_too_long_for_a_text_bind_before_writing():
    kb, conn = _store()
    with pytest.raises(PublishFailed) as ex:
        kb.publish(corpus_id_hex=CORPUS_ID, vertical="kbdoc-7", queue_keys=["HALAN"], chunks=_chunks(1),
                   vectors=[[_WORST_CASE] * 2200], embedding_model="m", dimension=2200)
    assert ex.value.code == "invalid_config" and "2200 dimensions" in ex.value.reason
    assert conn.statements == []  # nothing was sent


# ---- health details are written by their own UPDATE (ORA-03146 in the MERGE above 32 KB) ---------


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, "HEALTHY", "FAILED"])
async def test_health_save_writes_details_outside_the_merge(previous):
    db = RecordingDatabase()  # validates that every statement's binds match its placeholders exactly
    db.on(r"FOR UPDATE", {"status": previous} if previous else None)
    details = {"integrity_problems": [{"filename": "ملف " * 60, "id": i} for i in range(200)]}
    changed = await HealthRepo(db).save(
        CheckResult("knowledge_sync", "FAILED", "3 published documents lost their chunks", details=details),
        label="Knowledge sync", checked_at=datetime(2026, 9, 25, 12, 0, 0),
    )
    assert changed is (previous != "FAILED")
    (merge_sql, merge_binds), = db.sql_matching(r"^MERGE INTO AIVA_health_checks")
    assert "details_json" not in merge_sql and "details" not in merge_binds
    (update_sql, update_binds), = db.sql_matching(r"^UPDATE AIVA_health_checks SET details_json")
    assert update_sql == "UPDATE AIVA_health_checks SET details_json = :details WHERE component_key = :component_key"
    assert json.loads(update_binds["details"]) == details and update_binds["component_key"] == "knowledge_sync"
    assert len(update_binds["details"].encode("utf-8")) > 32767
    verbs = [s.split()[0] for s, _ in db.statements]
    assert verbs[:3] == ["SELECT", "MERGE", "UPDATE"]
    assert verbs[3:] == ([] if previous == "FAILED" else ["INSERT"])
    assert db.commits == 1 and db.rollbacks == 0  # one transaction
