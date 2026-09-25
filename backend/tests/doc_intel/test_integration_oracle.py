"""Opt-in integration tests against the configured Oracle schema (``DOC_INTEL_IT=1``).

The schema is shared with live production, so these tests follow strict isolation rules.

* Nothing is ever committed. Each test gets its own sessions: the app side (``KbRepo``,
  ``HealthRepo``) runs on one async connection, the knowledge-base side (``KbStore``) on one
  sync connection. Every transaction the code under test opens becomes a SAVEPOINT, rolled
  back to on error exactly as the real context managers roll back, and each session is
  rolled back when its test ends, pass or fail. If the run crashes, the database rolls the
  dead sessions back. Other sessions (production traffic, other harnesses) never see a test
  row. No DDL runs (DDL would commit).
* Rows are written only to AIVA_KB_DOCUMENTS, AIVA_HEALTH_CHECKS, AIVA_HEALTH_CHECK_EVENTS,
  DI_TEST_KB_CORPUS and DI_TEST_KB_CHUNK, and carry this run's markers: ``account_id`` and
  ``uploaded_by`` in 990000100-990000999, a random DI_TEST corpus id per test, and health
  component keys ``di-it-<run>-...``. Everything else is only read.
* ``claim_next`` (the oldest QUEUED row of the whole table) and ``recover_stale`` (every stale
  PROCESSING row) run only after a read-only guard shows they can reach this run's rows only.
* Audit and error logging are off and the embedder is a fake, so nothing reaches
  AIVA_audit_logs / AIVA_error_logs and no document text leaves the machine.

Run from AIVA-V2 (PowerShell: ``$env:DOC_INTEL_IT = "1"`` first):

    DOC_INTEL_IT=1 python -m pytest backend/tests/doc_intel/test_integration_oracle.py -q -p no:cacheprovider -rs

Without ``DOC_INTEL_IT=1`` every test is skipped. With it, one connection attempt (<= 5 s)
decides: an unreachable database or missing tables skip the module with the reason.
The last test re-counts this run's markers from a fresh session and must find zero rows.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import random
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import oracledb
import pytest
import pytest_asyncio
from fastapi import UploadFile

from backend.config import Settings
from backend.database import Database
from backend.doc_intel.chunking import PreparedChunk
from backend.doc_intel.constants import (
    DOC_FAILED,
    DOC_PROCESSING,
    DOC_PUBLISHED,
    DOC_QUEUED,
    KB_CHUNKER_VERSION,
    KB_PAYLOAD_SOURCE,
    KB_STAGES,
    STAGE_COMPLETED,
    STAGE_FAILED,
    STAGE_PENDING,
    STAGE_RUNNING,
    STAGE_SKIPPED,
)
from backend.doc_intel.health import CheckResult, HealthDeps, _integrity_problems
from backend.doc_intel.health_repo import HealthRepo
from backend.doc_intel.kb_import import KbImportService
from backend.doc_intel.kb_repo import INTERRUPTED_REASON, KbRepo, loads_json, parse_utc, row_to_out
from backend.doc_intel.kb_store import KbStore, KbTables, PublishFailed
from backend.doc_intel.queue_config import queues_with_vertical
from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.textutil import utc_now
from backend.services.kb_queue_groups import resolve_verticals
from embedding_service.embedders.result import EmbedBatchResult

from . import _doc_fixtures as fx
from .conftest import make_user

pytestmark = pytest.mark.skipif(
    os.environ.get("DOC_INTEL_IT") != "1",
    reason="opt-in: set DOC_INTEL_IT=1 to run the Oracle integration tests",
)

# ---- this run's markers ----------------------------------------------------------------------

RUN = uuid.uuid4().hex[:10]
MARKER_ACCOUNT = 990000100 + random.SystemRandom().randrange(900)  # 990000100..990000999
HEALTH_KEY_PREFIX = f"di-it-{RUN}"
RUN_BATCH = uuid.uuid4().hex  # VARCHAR2(32)
USED_CORPORA: list[str] = []  # every DI_TEST corpus id this run created (for the final count)

T = "AIVA_kb_documents"
DI_TEST_TABLES = KbTables(corpus="DI_TEST_KB_CORPUS", chunk="DI_TEST_KB_CHUNK")
WRITABLE_TABLES = ("AIVA_KB_DOCUMENTS", "AIVA_HEALTH_CHECKS", "AIVA_HEALTH_CHECK_EVENTS", "DI_TEST_KB_CORPUS", "DI_TEST_KB_CHUNK")
DIM = 1536
CALL_TIMEOUT_MS = 90_000  # no statement may hang a run (the corpus row lock waits 30 s)
PDF_MIME = "application/pdf"
WORKER = f"di-it-worker-{RUN}"
ANCIENT = datetime(2000, 1, 1)  # backdating: older than any real row
ARABIC_WORDS = "هذا نص عربي طويل لاختبار حدود الأعمدة في قاعدة البيانات "


def new_corpus_id() -> str:
    cid = uuid.uuid4().hex
    USED_CORPORA.append(cid)
    return cid


# ---- connections -------------------------------------------------------------------------------


def _connect_kwargs(s: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user": s.oracle_user,
        "password": s.oracle_password,
        "dsn": s.oracle_dsn,
        "tcp_connect_timeout": 5,
        "retry_count": 0,
    }
    if s.oracle_wallet_dir:
        kwargs.update(config_dir=s.oracle_wallet_dir, wallet_location=s.oracle_wallet_dir)
        if s.oracle_wallet_password:
            kwargs["wallet_password"] = s.oracle_wallet_password
    return kwargs


def _error_code(ex: BaseException) -> str:
    """The DPY-/ORA- code of an error; never the message (it may name the host)."""
    first = (str(ex).strip().splitlines() or [type(ex).__name__])[0]
    return first.split(":", 1)[0][:20]


@pytest.fixture(scope="session")
def oracle() -> dict[str, Any]:
    """Connection settings of the configured schema; skips the module when it is unusable."""
    kwargs = _connect_kwargs(Settings())
    try:
        conn = oracledb.connect(**kwargs)
    except oracledb.Error as ex:
        pytest.skip(f"DOC_INTEL_IT=1 but the database is unreachable ({_error_code(ex)})")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM user_tables WHERE table_name IN (:t0, :t1, :t2, :t3, :t4)",
                {f"t{i}": name for i, name in enumerate(WRITABLE_TABLES)},
            )
            found = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    missing = sorted(set(WRITABLE_TABLES) - found)
    if missing:
        pytest.skip(f"tables missing in the connected schema (migration V001 / DI_TEST fixture): {', '.join(missing)}")
    return kwargs


class TxDatabase(Database):
    """``backend.database.Database`` on ONE async connection whose work is never committed.

    ``connection()`` opens a savepoint instead of a transaction and rolls back to it on
    error, like the real one rolls back; the fixture rolls the whole session back.
    """

    def __init__(self, conn: oracledb.AsyncConnection) -> None:  # no pool, no settings
        self._conn = conn
        self._savepoints = itertools.count(1)

    @property
    def raw(self) -> oracledb.AsyncConnection:
        return self._conn

    async def _run(self, sql: str) -> None:
        cur = self._conn.cursor()
        try:
            await cur.execute(sql)
        finally:
            cur.close()

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[oracledb.AsyncConnection]:
        name = f"di_it_app_{next(self._savepoints)}"
        await self._run(f"SAVEPOINT {name}")
        try:
            yield self._conn
        except Exception:
            await self._run(f"ROLLBACK TO SAVEPOINT {name}")
            raise

    async def fetch_one(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None) -> Any:
        return await super().fetch_one(sql, params, conn=conn or self._conn)

    async def fetch_all(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None) -> Any:
        return await super().fetch_all(sql, params, conn=conn or self._conn)

    async def execute(self, sql: str, params: dict[str, Any] | None = None, *, conn: Any = None,
                      return_id: bool = False) -> Any:
        return await super().execute(sql, params, conn=conn or self._conn, return_id=return_id)


@pytest_asyncio.fixture
async def tx_db(oracle: dict[str, Any]) -> AsyncIterator[TxDatabase]:
    conn = await oracledb.connect_async(**oracle)
    conn.call_timeout = CALL_TIMEOUT_MS
    try:
        yield TxDatabase(conn)
    finally:
        try:
            await conn.rollback()
        finally:
            await conn.close()


class KbSession:
    """One sync connection standing in for the KB pool; ``factory`` is KbStore's connection factory.

    Each KbStore call runs under its own savepoint (rolled back to on error, as the real
    ``EmbeddingService.db.connection`` rolls back); nothing is ever committed.
    """

    def __init__(self, conn: oracledb.Connection) -> None:
        self.conn = conn
        self._savepoints = itertools.count(1)

    @contextmanager
    def factory(self) -> Iterator[oracledb.Connection]:
        name = f"di_it_kb_{next(self._savepoints)}"
        with self.conn.cursor() as cur:
            cur.execute(f"SAVEPOINT {name}")
        try:
            yield self.conn
        except Exception:
            with self.conn.cursor() as cur:
                cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
            raise

    def store(self) -> KbStore:
        return KbStore(self.factory, DI_TEST_TABLES)

    def seed_corpus(self, config: dict[str, Any], *, as_oson: bool = False) -> str:
        """Insert a DI_TEST corpus row (JSON text, so numbers are stored as NUMBER; or native OSON)."""
        cid = new_corpus_id()
        binds = {"i": bytes.fromhex(cid), "n": f"DI IT {RUN}", "s": f"di-it-{cid[:24]}"}
        with self.conn.cursor() as cur:
            if as_oson:
                cur.setinputsizes(c=oracledb.DB_TYPE_JSON)
                cur.execute(
                    "INSERT INTO DI_TEST_KB_CORPUS (corpus_id, name, slug, config_json) VALUES (:i, :n, :s, :c)",
                    {**binds, "c": config},
                )
            else:
                cur.execute(
                    "INSERT INTO DI_TEST_KB_CORPUS (corpus_id, name, slug, config_json) VALUES (:i, :n, :s, CAST(:c AS JSON))",
                    {**binds, "c": json.dumps(config, ensure_ascii=False)},
                )
        return cid

    def raw_config(self, cid: str) -> Any:
        with self.conn.cursor() as cur:
            cur.execute("SELECT config_json FROM DI_TEST_KB_CORPUS WHERE corpus_id = :i", {"i": bytes.fromhex(cid)})
            row = cur.fetchone()
        return None if row is None else row[0]

    def chunk_rows(self, cid: str, vertical: str) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, chunk_index, chunker_version, content_hash, chunk_text, payload_json,
                       embedding_model, VECTOR_DIMENSION_COUNT(embedding) AS dims, updated_at
                FROM DI_TEST_KB_CHUNK
                WHERE corpus_id = :c AND external_parent_id = :p
                ORDER BY chunk_index
                """,
                {"c": bytes.fromhex(cid), "p": vertical},
            )
            cols = [d[0].lower() for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            for rec in rows:
                if hasattr(rec["chunk_text"], "read"):
                    rec["chunk_text"] = rec["chunk_text"].read()
        return rows

    def embedding(self, cid: str, vertical: str, index: int) -> list[float]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT embedding FROM DI_TEST_KB_CHUNK WHERE corpus_id = :c AND external_parent_id = :p AND chunk_index = :i",
                {"c": bytes.fromhex(cid), "p": vertical, "i": index},
            )
            (value,) = cur.fetchone()
        return [float(x) for x in value]

    def search(self, cid: str, query: list[float], verticals: list[str] | None) -> list[tuple[str, int, float]]:
        """The chat-retrieval filter (``JSON_VALUE(payload_json,'$.vertical') IN (...)``) plus vector distance."""
        if not verticals:
            return []
        binds: dict[str, Any] = {"c": bytes.fromhex(cid), "q": json.dumps(query)}
        names = []
        for i, v in enumerate(verticals):
            binds[f"v{i}"] = v
            names.append(f":v{i}")
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT c.external_parent_id, c.chunk_index,
                       VECTOR_DISTANCE(c.embedding, VECTOR(:q, {DIM}, FLOAT32), COSINE) AS dist
                FROM DI_TEST_KB_CHUNK c
                WHERE c.corpus_id = :c AND c.embedding IS NOT NULL
                  AND JSON_VALUE(c.payload_json, '$.vertical' RETURNING VARCHAR2(256)) IN ({", ".join(names)})
                ORDER BY dist
                FETCH FIRST 10 ROWS ONLY
                """,
                binds,
            )
            return [(str(p), int(i), float(d)) for p, i, d in cur.fetchall()]


@pytest.fixture
def kb_session(oracle: dict[str, Any]) -> Iterator[KbSession]:
    conn = oracledb.connect(**oracle)
    conn.call_timeout = CALL_TIMEOUT_MS
    try:
        yield KbSession(conn)
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


# ---- data helpers ------------------------------------------------------------------------------


def corpus_config(**overrides: Any) -> dict[str, Any]:
    """Queue groups with spaces in their keys, an unknown key, nulls, and numbers the JSON
    column hands back as Decimal."""
    cfg: dict[str, Any] = {
        "adapter": "generic_jsonl_v1",
        "chunker_version": "1",
        "chunk_max_chars": 1200,
        "chunk_overlap": 120,
        "embedder": {
            "type": "http",
            "model": "text-embedding-3-small",
            "base_url": "https://embeddings.invalid/v1",
            "api_key_env": "DI_IT_NEVER_SET",
            "dimension": DIM,
            "pricing_usd_per_million_tokens": 0.02,
        },
        "queue_groups": {
            "Credit Support": {"label": "Credit Support", "verticals": ["Credit"], "ivr_hint": "press 2"},
            "Card Support": {"label": "Card Support", "verticals": []},
            "HALAN": {"label": "Halan", "verticals": ["CF", "Pay"]},
        },
        "unknown_extra": {"nested": [1, 2.5, {"deep": "value"}], "flag": True, "none": None},
        "retrieval_weight": 0.75,
        "big_number": 1234567890123,
    }
    cfg.update(overrides)
    return cfg


def without_queue_groups(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cfg.items() if k != "queue_groups"}


def _contains_decimal(value: Any) -> bool:
    if isinstance(value, Decimal):
        return True
    if isinstance(value, dict):
        return any(_contains_decimal(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_decimal(v) for v in value)
    return False


def unit_vector(i: int) -> list[float]:
    vec = [0.001] * DIM
    vec[i % DIM] = 1.0
    return vec


def prepared_chunks(vertical: str, n: int, *, version: str = "v1", doc_id: int = 1) -> list[PreparedChunk]:
    out: list[PreparedChunk] = []
    for i in range(n):
        text = f"[Document: IT دليل.pdf · Section: Cards · Page {i + 1}]\n{version} chunk {i}: {ARABIC_WORDS}"
        payload = {
            "vertical": vertical,
            "source": KB_PAYLOAD_SOURCE,
            "doc_id": doc_id,
            "filename": "IT دليل.pdf",
            "section": "Cards",
            "pages": [i + 1],
            "queues": ["Card Support"],
            "chunk_index": i,
        }
        out.append(
            PreparedChunk(
                index=i,
                text=text,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                pages=(i + 1,),
                section="Cards",
                payload=payload,
            )
        )
    return out


def publish(kb: KbStore, cid: str, vertical: str, queues: list[str], chunks: list[PreparedChunk],
            vectors: list[list[float]] | None = None) -> dict[str, Any]:
    return kb.publish(
        corpus_id_hex=cid,
        vertical=vertical,
        queue_keys=queues,
        chunks=chunks,
        vectors=vectors if vectors is not None else [unit_vector(i) for i in range(len(chunks))],
        embedding_model="di-it-fake-embedder",
        dimension=DIM,
    )


async def insert_doc(
    repo: KbRepo,
    *,
    status: str = DOC_QUEUED,
    stages: dict[str, str] | None = None,
    corpus_id: str | None = None,
    sha256: str | None = None,
    filename: str = "IT دليل البطاقات.pdf",
    queue_keys: tuple[str, ...] = ("Card Support",),
    batch_id: str = RUN_BATCH,
    **kw: Any,
) -> int:
    return await repo.insert_document(
        batch_id=batch_id,
        account_id=MARKER_ACCOUNT,
        corpus_id=corpus_id or uuid.uuid4().hex,
        queue_keys=list(queue_keys),
        filename=filename,
        status=status,
        stage_statuses=stages if stages is not None else {"upload": STAGE_COMPLETED},
        content_type=PDF_MIME,
        size_bytes=1234,
        sha256=sha256 or uuid.uuid4().hex * 2,
        uploaded_by=MARKER_ACCOUNT,
        **kw,
    )


async def force_processing(db: TxDatabase, doc_id: int, worker: str = WORKER) -> None:
    """What a claim does, aimed at this run's row only (claim_next takes the table's oldest)."""
    await db.execute(
        f"""
        UPDATE {T} SET status = 'PROCESSING', attempts = attempts + 1, worker_id = :w,
                       started_at = :now, finished_at = NULL, updated_at = :now
        WHERE id = :id AND account_id = :m
        """,
        {"w": worker, "now": utc_now(), "id": doc_id, "m": MARKER_ACCOUNT},
    )


async def backdate(db: TxDatabase, doc_id: int, column: str, when: datetime = ANCIENT) -> None:
    assert column in ("created_at", "updated_at", "started_at")
    await db.execute(
        f"UPDATE {T} SET {column} = :ts WHERE id = :id AND account_id = :m",
        {"ts": when, "id": doc_id, "m": MARKER_ACCOUNT},
    )


async def scalar(db: TxDatabase, sql: str, params: dict[str, Any] | None = None) -> Any:
    row = await db.fetch_one(sql, params)
    return None if row is None else next(iter(row.values()))


# ---- KbRepo against the real AIVA_KB_DOCUMENTS ------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_sets_the_vertical_and_reads_back_through_row_to_out(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    details = {"upload": {"started_at": "2026-09-25T10:00:00Z", "finished_at": "2026-09-25T10:00:01Z",
                          "metrics": {"size_bytes": 1234, "kind": "pdf"}}}
    doc_id = await insert_doc(repo, queue_keys=("Credit Support", "Card Support"), stage_details=details)
    row = await repo.get_document(doc_id)
    assert row["vertical"] == f"kbdoc-{doc_id}"
    assert row["account_id"] == MARKER_ACCOUNT and row["account_name"] is None  # no such account: LEFT JOINs
    assert row["status"] == DOC_QUEUED and row["attempts"] == 0 and row["worker_id"] is None
    assert row["upload_status"] == STAGE_COMPLETED and row["extraction_status"] == STAGE_PENDING
    out = row_to_out(row)
    assert out.filename == "IT دليل البطاقات.pdf"
    assert out.queue_keys == ["Credit Support", "Card Support"]
    assert out.stages[0].started_at == "2026-09-25T10:00:00Z"
    assert out.created_at and out.created_at.endswith("Z") and parse_utc(out.created_at) is not None
    assert abs((parse_utc(out.created_at) - utc_now()).total_seconds()) < 600  # stored as UTC


@pytest.mark.asyncio
async def test_list_documents_orders_counts_and_filters(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    other_batch = uuid.uuid4().hex
    a = await insert_doc(repo)
    b = await insert_doc(repo, batch_id=other_batch)
    c = await insert_doc(
        repo, status=DOC_FAILED, stages={"upload": STAGE_FAILED, **{s: STAGE_SKIPPED for s in KB_STAGES[1:]}},
        failed_stage="upload", error_message="Unsupported file type", finished=True,
    )
    rows, total = await repo.list_documents(account_id=MARKER_ACCOUNT, limit=50)
    assert total == 3 and [r["id"] for r in rows] == [c, b, a]  # created_at DESC, id DESC
    rows, total = await repo.list_documents(account_id=MARKER_ACCOUNT, limit=2, offset=1)
    assert total == 3 and [r["id"] for r in rows] == [b, a]
    rows, total = await repo.list_documents(account_id=MARKER_ACCOUNT, status=DOC_FAILED)
    assert total == 1 and rows[0]["id"] == c and rows[0]["finished_at"] is not None
    rows, total = await repo.list_documents(account_id=MARKER_ACCOUNT, batch_id=other_batch)
    assert total == 1 and rows[0]["id"] == b
    positions = await repo.queue_positions()
    assert positions[a] < positions[b] and c not in positions


@pytest.mark.asyncio
async def test_claim_next_is_a_conditional_update(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    doc_id = await insert_doc(repo)
    await backdate(tx_db, doc_id, "created_at")
    oldest = await scalar(tx_db, f"SELECT id FROM {T} WHERE status = 'QUEUED' ORDER BY created_at, id FETCH FIRST 1 ROWS ONLY")
    if oldest != doc_id:
        pytest.skip("another QUEUED row is older than this run's row; claim_next would reach it")
    row = await repo.claim_next(WORKER)
    assert row is not None and row["id"] == doc_id
    assert row["status"] == DOC_PROCESSING and row["attempts"] == 1 and row["worker_id"] == WORKER
    assert row["started_at"] is not None and row["finished_at"] is None
    # The claim's UPDATE is conditional on QUEUED: repeating it now changes nothing.
    cur = tx_db.raw.cursor()
    try:
        await cur.execute(
            f"UPDATE {T} SET status = 'PROCESSING', attempts = attempts + 1 WHERE id = :id AND status = 'QUEUED'",
            {"id": doc_id},
        )
        assert cur.rowcount == 0
    finally:
        cur.close()
    assert await scalar(tx_db, f"SELECT attempts FROM {T} WHERE id = :id", {"id": doc_id}) == 1
    # Only when nobody else has a QUEUED row can "nothing to claim" be asked safely.
    if await scalar(tx_db, f"SELECT COUNT(*) AS n FROM {T} WHERE status = 'QUEUED'") == 0:
        assert await repo.claim_next(WORKER) is None


@pytest.mark.asyncio
async def test_set_stage_merges_details_and_truncates_long_arabic_text(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    doc_id = await insert_doc(repo)
    assert await repo.set_stage(doc_id, "extraction", STAGE_RUNNING) is False  # not PROCESSING: refused
    await force_processing(tx_db, doc_id)

    metrics = {"pages": 3, "blocks": 12, "note": ARABIC_WORDS}
    assert await repo.set_stage(doc_id, "extraction", STAGE_RUNNING)
    assert await repo.set_stage(doc_id, "extraction", STAGE_COMPLETED, metrics=metrics)
    warnings = [{"code": "low_text", "message": ARABIC_WORDS, "page": i} for i in range(1, 4)]
    assert await repo.set_results(doc_id, page_count=3, warnings=warnings)
    long_error = "فشل التقسيم: " + ARABIC_WORDS * 150  # ~16 KB of UTF-8
    assert await repo.set_stage(doc_id, "chunking", STAGE_RUNNING)
    assert await repo.set_stage(doc_id, "chunking", STAGE_FAILED, error=long_error)

    row = await repo.get_document(doc_id)
    assert row["chunking_status"] == STAGE_FAILED and row["failed_stage"] == "chunking"
    assert row["error_message"].endswith("...") and long_error.startswith(row["error_message"][:-3])
    assert await scalar(tx_db, f"SELECT LENGTHB(error_message) AS b FROM {T} WHERE id = :id", {"id": doc_id}) <= 4000
    details = loads_json(row["stage_details"], {})
    assert details["extraction"]["metrics"] == metrics  # merged, not overwritten by later stages
    assert details["extraction"]["finished_at"].endswith("Z") and "seconds" in details["extraction"]
    assert len(details["chunking"]["error"].encode("utf-8")) <= 4000
    assert set(details) == {"extraction", "chunking"}
    assert loads_json(row["warnings_json"], []) == warnings and row["page_count"] == 3
    out = row_to_out(row)
    assert out.stages[2].status == STAGE_FAILED and out.stages[2].error == details["chunking"]["error"]
    assert [w.page for w in out.warnings] == [1, 2, 3]


@pytest.mark.asyncio
async def test_stage_details_and_warnings_over_32k(tx_db: TxDatabase):
    """Probe: stage_details / warnings_json are CLOBs bound as one string; > 32 KB must be stored.

    Publishing stores the queue before/after lists in stage_details, and up to 200 warnings of
    400 characters each are kept, so both can pass 32 KB for real documents.
    """
    repo = KbRepo(tx_db)
    doc_id = await insert_doc(repo)
    await force_processing(tx_db, doc_id)
    big_metrics = {"queues": {"Card Support": {"before": [f"kbdoc-{i}" for i in range(4000)], "after": []}}}
    warnings = [{"code": "low_text", "message": ARABIC_WORDS * 7, "page": i} for i in range(200)]
    assert len(json.dumps(big_metrics).encode()) > 32767
    assert len(json.dumps(warnings, ensure_ascii=False).encode()) > 32767
    assert await repo.set_stage(doc_id, "extraction", STAGE_COMPLETED, metrics=big_metrics)
    assert await repo.set_results(doc_id, warnings=warnings)
    row = await repo.get_document(doc_id)
    assert loads_json(row["stage_details"], {})["extraction"]["metrics"] == big_metrics
    assert loads_json(row["warnings_json"], []) == warnings


@pytest.mark.asyncio
async def test_finish_published_and_failed(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    published = await insert_doc(repo)
    await force_processing(tx_db, published)
    summary = {"chunks_written": 3, "queues": {"Card Support": {"before": [], "after": [f"kbdoc-{published}"]}}}
    assert await repo.finish(published, status=DOC_PUBLISHED, metrics=summary, page_count=2, chunk_count=3,
                             tokens_used=120, cost_usd=0.0000024)
    row = await repo.get_document(published)
    assert row["status"] == DOC_PUBLISHED and row["publishing_status"] == STAGE_COMPLETED
    assert row["published_at"] and row["finished_at"] and row["failed_stage"] is None and row["error_message"] is None
    assert (row["chunk_count"], row["tokens_used"], row["page_count"]) == (3, 120, 2)
    assert row_to_out(row).cost_usd == pytest.approx(0.0000024)
    assert loads_json(row["stage_details"], {})["publishing"]["metrics"] == summary
    assert await repo.finish(published, status=DOC_FAILED, stage="publishing", error="late") is False  # left PROCESSING

    failed = await insert_doc(repo)
    await force_processing(tx_db, failed)
    assert await repo.finish(failed, status=DOC_FAILED, stage="embedding", error="Embedding provider rejected the API key (401)")
    row = await repo.get_document(failed)
    assert (row["status"], row["failed_stage"], row["embedding_status"]) == (DOC_FAILED, "embedding", STAGE_FAILED)
    assert row["error_message"] == "Embedding provider rejected the API key (401)" and row["published_at"] is None


@pytest.mark.asyncio
async def test_reset_for_retry_republish_unpublish_and_queue_updates(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    doc_id = await insert_doc(repo)
    await force_processing(tx_db, doc_id)
    for stage in ("extraction", "chunking"):
        await repo.set_stage(doc_id, stage, STAGE_RUNNING)
    await repo.set_stage(doc_id, "extraction", STAGE_COMPLETED)
    assert await repo.finish(doc_id, status=DOC_FAILED, stage="chunking", error="Document produced no text chunks")

    assert await repo.reset_for_retry(doc_id, "chunking")
    row = await repo.get_document(doc_id)
    assert row["status"] == DOC_QUEUED and row["failed_stage"] is None and row["error_message"] is None
    assert row["extraction_status"] == STAGE_COMPLETED and row["chunking_status"] == STAGE_PENDING
    assert set(loads_json(row["stage_details"], {})) == {"extraction"}
    assert await repo.reset_for_retry(doc_id, "chunking") is False  # QUEUED, not FAILED

    assert await repo.update_queue_keys(doc_id, ["HALAN"], statuses=(DOC_QUEUED, DOC_FAILED))
    assert await repo.update_queue_keys(doc_id, ["Card Support"], statuses=(DOC_PUBLISHED,)) is False
    await force_processing(tx_db, doc_id)
    assert await repo.finish(doc_id, status=DOC_PUBLISHED, chunk_count=1)
    assert await repo.reset_for_republish(doc_id)
    row = await repo.get_document(doc_id)
    assert row["status"] == DOC_QUEUED and row["publishing_status"] == STAGE_PENDING
    assert row["extraction_status"] == STAGE_COMPLETED and loads_json(row["queue_keys"], []) == ["HALAN"]
    assert await repo.mark_unpublished(doc_id) is False  # QUEUED
    await force_processing(tx_db, doc_id)
    assert await repo.finish(doc_id, status=DOC_PUBLISHED, chunk_count=1)
    assert await repo.mark_unpublished(doc_id)
    assert (await repo.get_document(doc_id))["status"] == "UNPUBLISHED"


@pytest.mark.asyncio
async def test_recover_stale_touch_and_release_interrupted(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    stale = await insert_doc(repo)
    await force_processing(tx_db, stale)
    await repo.set_stage(stale, "extraction", STAGE_RUNNING)
    await backdate(tx_db, stale, "updated_at")
    threshold = ANCIENT + timedelta(days=1)
    foreign = await scalar(
        tx_db,
        f"SELECT COUNT(*) AS n FROM {T} WHERE status = 'PROCESSING' AND updated_at < :t AND account_id <> :m",
        {"t": threshold, "m": MARKER_ACCOUNT},
    )
    if foreign:
        pytest.skip("another stale PROCESSING row exists; recover_stale would reach it")
    assert await repo.recover_stale(threshold) == [stale]
    row = await repo.get_document(stale)
    assert (row["status"], row["failed_stage"], row["extraction_status"]) == (DOC_FAILED, "extraction", STAGE_FAILED)
    assert row["error_message"] == INTERRUPTED_REASON and row["finished_at"] is not None
    assert await repo.recover_stale(threshold) == []

    live = await insert_doc(repo)
    await force_processing(tx_db, live, worker="di-it-other")
    await backdate(tx_db, live, "updated_at")
    await repo.touch(live, "someone-else")  # another worker's heartbeat changes nothing
    assert parse_utc(await scalar(tx_db, f"SELECT updated_at FROM {T} WHERE id = :id", {"id": live})) == ANCIENT
    await repo.touch(live, "di-it-other")
    assert parse_utc(await scalar(tx_db, f"SELECT updated_at FROM {T} WHERE id = :id", {"id": live})) > ANCIENT
    assert await repo.release_interrupted(live, "someone-else") is False
    assert await repo.release_interrupted(live, "di-it-other") is True
    row = await repo.get_document(live)
    # No stage was RUNNING: the first stage still to do (extraction) is the one marked failed.
    assert (row["status"], row["failed_stage"], row["extraction_status"]) == (DOC_FAILED, "extraction", STAGE_FAILED)
    assert row["error_message"] == INTERRUPTED_REASON


@pytest.mark.asyncio
async def test_find_active_duplicate(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    corpus, other_corpus, sha = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex * 2
    await insert_doc(
        repo, status=DOC_FAILED, stages={"upload": STAGE_FAILED, **{s: STAGE_SKIPPED for s in KB_STAGES[1:]}},
        corpus_id=corpus, sha256=sha, failed_stage="upload", error_message="rejected", finished=True,
    )
    assert await repo.find_active_duplicate(corpus, sha) is None  # a failed upload is no duplicate
    queued = await insert_doc(repo, corpus_id=corpus, sha256=sha)
    assert await repo.find_active_duplicate(corpus, sha) == queued
    assert await repo.find_active_duplicate(other_corpus, sha) is None


@pytest.mark.asyncio
async def test_monitoring_queries(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    failed = await insert_doc(repo)
    await force_processing(tx_db, failed)
    await repo.finish(failed, status=DOC_FAILED, stage="embedding", error="Embedding endpoint unavailable (503)")
    published = await insert_doc(repo, queue_keys=("Card Support", "HALAN"))
    await force_processing(tx_db, published)
    await repo.finish(published, status=DOC_PUBLISHED, chunk_count=4)
    stuck = await insert_doc(repo)
    await force_processing(tx_db, stuck)
    await backdate(tx_db, stuck, "started_at")

    counts = await repo.status_counts()
    assert counts[DOC_FAILED] >= 1 and counts[DOC_PUBLISHED] >= 1 and counts[DOC_PROCESSING] >= 1
    assert stuck in [int(r["id"]) for r in await repo.stuck_documents(ANCIENT + timedelta(days=1))]
    n, recent = await repo.failed_since(utc_now() - timedelta(hours=1))
    assert n >= 1 and failed in [int(r["id"]) for r in recent]
    rows = {int(r["id"]): r for r in await repo.failures(1)}
    assert rows[failed]["failed_stage"] == "embedding" and rows[failed]["account_name"] is None
    assert {failed, published, stuck} <= {int(r["id"]) for r in await repo.activity(500)}
    mine = [r for r in await repo.published_documents() if int(r["id"]) == published]
    assert len(mine) == 1 and mine[0]["vertical"] == f"kbdoc-{published}" and mine[0]["chunk_count"] == 4
    assert loads_json(mine[0]["queue_keys"], []) == ["Card Support", "HALAN"]


@pytest.mark.asyncio
async def test_check_constraints_reject_bad_statuses(tx_db: TxDatabase):
    repo = KbRepo(tx_db)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await insert_doc(repo, status="BOGUS")
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await insert_doc(repo, stages={"upload": "DONE"})
    doc_id = await insert_doc(repo)
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo._update(doc_id, stages=[], columns={"status": "NOPE"}, expect_status=(DOC_QUEUED,))
    # The failed statements were rolled back; the session and the good row are intact.
    assert (await repo.get_document(doc_id))["status"] == DOC_QUEUED
    assert await scalar(tx_db, f"SELECT COUNT(*) AS n FROM {T} WHERE account_id = :m", {"m": MARKER_ACCOUNT}) == 1


# ---- HealthRepo against the real AIVA_HEALTH_CHECKS / _EVENTS -------------------------------------------


def row_after_recovery_has_no_action(rows: list[dict[str, Any]], key: str) -> bool:
    """A HEALTHY result replaces the stored reason and clears the suggested action."""
    row = {r["component_key"]: r for r in rows}[key]
    return row["status"] == "HEALTHY" and row["reason"] == "recovered" and row["suggested_action"] is None


@pytest.mark.asyncio
async def test_health_repo_upserts_and_records_transitions(tx_db: TxDatabase):
    repo = HealthRepo(tx_db)
    key = f"{HEALTH_KEY_PREFIX}-probe"
    t0 = utc_now().replace(microsecond=0)
    at = [t0 + timedelta(seconds=i) for i in range(5)]
    long_reason = "قاعدة البيانات غير متاحة: " + ARABIC_WORDS * 100
    assert await repo.save(CheckResult(key, "HEALTHY", "ok", latency_ms=12, details={"x": 1}), label="IT probe", checked_at=at[0])
    assert await repo.save(CheckResult(key, "HEALTHY", "still ok"), label="IT probe", checked_at=at[1]) is False
    assert await repo.save(
        CheckResult(key, "FAILED", long_reason, suggested_action="Check the DB host. " * 200, details={"note": ARABIC_WORDS}),
        label="IT probe", checked_at=at[2],
    )
    row = {r["component_key"]: r for r in await repo.load_all()}[key]
    assert len(row["reason"].encode("utf-8")) <= 4000 and row["reason"].endswith("...")
    assert len(row["suggested_action"].encode("utf-8")) <= 2000
    assert loads_json(row["details_json"], {}) == {"note": ARABIC_WORDS} and row["consecutive_failures"] == 1
    assert await repo.save(CheckResult(key, "FAILED", "still down"), label="IT probe", checked_at=at[3]) is False

    row = {r["component_key"]: r for r in await repo.load_all()}[key]
    assert row["status"] == "FAILED" and row["consecutive_failures"] == 2
    assert parse_utc(row["last_success_at"]) == at[1] and parse_utc(row["last_failure_at"]) == at[3]
    assert parse_utc(row["checked_at"]) == at[3] and row["reason"] == "still down"

    assert await repo.save(CheckResult(key, "HEALTHY", "recovered"), label="IT probe", checked_at=at[4])
    row = {r["component_key"]: r for r in await repo.load_all()}[key]
    assert row["consecutive_failures"] == 0 and parse_utc(row["last_success_at"]) == at[4]

    events = [e for e in await repo.list_events(500) if e["component_key"] == key]
    assert [(e["old_status"], e["new_status"]) for e in events] == [
        ("FAILED", "HEALTHY"), ("HEALTHY", "FAILED"), (None, "HEALTHY"),
    ]
    failed_event = events[1]
    assert len(failed_event["reason"].encode("utf-8")) <= 4000 and failed_event["reason"].endswith("...")

    assert row_after_recovery_has_no_action(await repo.load_all(), key)

    other = f"{HEALTH_KEY_PREFIX}-bad"
    with pytest.raises(oracledb.DatabaseError, match="ORA-02290"):
        await repo.save(CheckResult(other, "BROKEN", "not a status"), label="IT probe", checked_at=t0)
    assert other not in {r["component_key"] for r in await repo.load_all()}


@pytest.mark.asyncio
async def test_health_repo_stores_details_over_32k(tx_db: TxDatabase):
    """Probe: the MERGE binds details_json as one string; > 32 KB must still be stored."""
    repo = HealthRepo(tx_db)
    key = f"{HEALTH_KEY_PREFIX}-big"
    details = {"integrity_problems": [{"filename": ARABIC_WORDS * 4, "id": i} for i in range(200)]}
    assert len(json.dumps(details, ensure_ascii=False).encode("utf-8")) > 32767
    assert await repo.save(CheckResult(key, "FAILED", "big details", details=details), label="IT probe", checked_at=utc_now())
    row = {r["component_key"]: r for r in await repo.load_all()}[key]
    assert loads_json(row["details_json"], {}) == details


# ---- KbStore against DI_TEST_KB_CORPUS / DI_TEST_KB_CHUNK --------------------------------------------------


def test_corpus_config_round_trip_keeps_unknown_keys_and_plain_numbers(kb_session: KbSession):
    cfg = corpus_config()
    cid = kb_session.seed_corpus(cfg)
    raw = kb_session.raw_config(cid)
    assert isinstance(raw, dict)  # native JSON column
    for number in (raw["retrieval_weight"], raw["big_number"], raw["unknown_extra"]["nested"][1]):
        assert isinstance(number, (Decimal, int, float))
    plain = kb_session.store().get_corpus_config(cid)
    assert plain == cfg and not _contains_decimal(plain)
    assert isinstance(plain["big_number"], int) and isinstance(plain["retrieval_weight"], float)
    assert kb_session.store().get_corpus_config(uuid.uuid4().hex) is None


def test_publish_writes_chunks_and_puts_the_vertical_in_exactly_the_selected_queues(kb_session: KbSession):
    cfg = corpus_config()
    cid = kb_session.seed_corpus(cfg)
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-1"
    chunks = prepared_chunks(vertical, 3)
    vectors = [unit_vector(i) for i in range(3)]
    summary = publish(kb, cid, vertical, ["Credit Support", "Card Support"], chunks, vectors)
    assert summary["chunks_written"] == 3 and summary["chunks_replaced"] == 0
    assert summary["materialized_default_queue_groups"] is False

    rows = kb_session.chunk_rows(cid, vertical)
    assert [r["chunk_index"] for r in rows] == [0, 1, 2]
    for rec, chunk in zip(rows, chunks):
        assert rec["chunk_text"] == chunk.text and rec["content_hash"] == chunk.content_hash
        assert rec["chunker_version"] == KB_CHUNKER_VERSION and rec["embedding_model"] == "di-it-fake-embedder"
        assert rec["dims"] == DIM
        assert rec["payload_json"] == chunk.payload  # vertical, source, doc_id, filename, section, pages, queues

    after = kb.get_corpus_config(cid)
    assert queues_with_vertical(after, vertical) == ["Card Support", "Credit Support"]
    assert after["queue_groups"]["HALAN"] == cfg["queue_groups"]["HALAN"]
    assert after["queue_groups"]["Credit Support"]["verticals"] == ["Credit", vertical]
    assert after["queue_groups"]["Credit Support"]["ivr_hint"] == "press 2"
    assert without_queue_groups(after) == without_queue_groups(cfg)  # every other key preserved exactly

    # Chat retrieval: verticals resolved from a selected queue find the document, nearest first ...
    for queue in ("Card Support", "Credit Support"):
        hits = kb_session.search(cid, vectors[0], resolve_verticals(after, [queue]))
        assert hits and hits[0][:2] == (vertical, 0) and hits[0][2] == pytest.approx(0.0, abs=1e-5)
        assert {h[0] for h in hits} == {vertical} and len(hits) == 3
    # ... and an unselected queue never sees it.
    assert resolve_verticals(after, ["HALAN"]) == ["CF", "Pay"]
    assert kb_session.search(cid, vectors[0], resolve_verticals(after, ["HALAN"])) == []


def test_republish_with_fewer_chunks_leaves_no_stale_chunks(kb_session: KbSession):
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-2"
    publish(kb, cid, vertical, ["Card Support"], prepared_chunks(vertical, 3))
    summary = publish(kb, cid, vertical, ["Card Support"], prepared_chunks(vertical, 2, version="v2"))
    assert summary["chunks_replaced"] == 3 and summary["chunks_written"] == 2
    rows = kb_session.chunk_rows(cid, vertical)
    assert [r["chunk_index"] for r in rows] == [0, 1] and all(r["chunk_text"].split("\n", 1)[1].startswith("v2") for r in rows)
    assert kb.get_corpus_config(cid)["queue_groups"]["Card Support"]["verticals"] == [vertical]  # added once


def test_set_queues_changes_configuration_only(kb_session: KbSession):
    cfg = corpus_config()
    cid = kb_session.seed_corpus(cfg)
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-3"
    publish(kb, cid, vertical, ["Card Support"], prepared_chunks(vertical, 2))
    before = kb_session.chunk_rows(cid, vertical)
    changes = kb.set_queues(corpus_id_hex=cid, vertical=vertical, queue_keys=["HALAN", "Credit Support"])
    after = kb.get_corpus_config(cid)
    assert queues_with_vertical(after, vertical) == ["Credit Support", "HALAN"]
    assert after["queue_groups"]["Card Support"]["verticals"] == []
    assert changes["queues"]["HALAN"] == {"before": ["CF", "Pay"], "after": ["CF", "Pay", vertical]}
    assert without_queue_groups(after) == without_queue_groups(cfg)
    assert kb_session.chunk_rows(cid, vertical) == before  # chunks, vectors and timestamps untouched


def test_unpublish_removes_the_vertical_everywhere_and_deletes_the_chunks(kb_session: KbSession):
    cfg = corpus_config()
    cid = kb_session.seed_corpus(cfg)
    kb = kb_session.store()
    vertical, keep = f"kbdoc-it-{RUN}-4", f"kbdoc-it-{RUN}-5"
    publish(kb, cid, vertical, ["Card Support", "HALAN"], prepared_chunks(vertical, 2))
    publish(kb, cid, keep, ["Card Support"], prepared_chunks(keep, 1))
    summary = kb.unpublish(corpus_id_hex=cid, vertical=vertical)
    assert summary == {"corpus_found": True, "removed_from_queues": ["Card Support", "HALAN"], "chunks_deleted": 2}
    after = kb.get_corpus_config(cid)
    assert queues_with_vertical(after, vertical) == [] and queues_with_vertical(after, keep) == ["Card Support"]
    assert after["queue_groups"]["HALAN"]["verticals"] == ["CF", "Pay"]
    assert without_queue_groups(after) == without_queue_groups(cfg)
    assert kb_session.chunk_rows(cid, vertical) == [] and len(kb_session.chunk_rows(cid, keep)) == 1
    assert kb.unpublish(corpus_id_hex=cid, vertical=vertical)["chunks_deleted"] == 0  # idempotent


def test_failed_publishes_leave_no_partial_writes(kb_session: KbSession):
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-6"
    publish(kb, cid, vertical, ["Card Support"], prepared_chunks(vertical, 2))
    config_before, rows_before = kb.get_corpus_config(cid), kb_session.chunk_rows(cid, vertical)

    with pytest.raises(PublishFailed) as unknown:
        publish(kb, cid, vertical, ["No Such Queue"], prepared_chunks(vertical, 3, version="v2"))
    assert unknown.value.code == "unknown_queue" and "No Such Queue" in unknown.value.reason
    assert kb.get_corpus_config(cid) == config_before and kb_session.chunk_rows(cid, vertical) == rows_before

    # A database error AFTER the old chunks were deleted: the whole publish must roll back.
    bad_vectors = [unit_vector(0), unit_vector(1), unit_vector(2)[:-1]]  # the third has 1535 dimensions
    with pytest.raises(PublishFailed) as broken:
        publish(kb, cid, vertical, ["HALAN"], prepared_chunks(vertical, 3, version="v3"), bad_vectors)
    assert broken.value.is_database_error and broken.value.reason.startswith("Knowledge base database error")
    assert kb.get_corpus_config(cid) == config_before
    assert kb_session.chunk_rows(cid, vertical) == rows_before


def test_publish_binds_a_vector_whose_json_text_exceeds_32k(kb_session: KbSession):
    """Some providers return full float64 precision: the JSON text of one 1536-dim vector can pass 32 KB."""
    rnd = random.Random(DIM)
    big = [rnd.uniform(-1, 1) * 10 ** -rnd.randint(0, 6) for _ in range(DIM)]
    assert len(json.dumps(big, separators=(",", ":"))) > 32767
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-7"
    publish(kb, cid, vertical, ["Card Support"], prepared_chunks(vertical, 2), [big, unit_vector(1)])
    stored = kb_session.embedding(cid, vertical, 0)
    assert stored == pytest.approx(big, rel=1e-6, abs=1e-30)  # FLOAT32 storage


def test_publish_stores_long_arabic_chunk_text(kb_session: KbSession):
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-8"
    chunks = prepared_chunks(vertical, 1)
    text = "[Document: دليل.pdf · Page 1]\n" + ARABIC_WORDS * 60  # > 4000 bytes of UTF-8
    assert len(text.encode("utf-8")) > 4000
    chunks = [PreparedChunk(index=0, text=text, content_hash=hashlib.sha256(text.encode()).hexdigest(), pages=(1,),
                            section=None, payload=chunks[0].payload)]
    publish(kb, cid, vertical, ["Card Support"], chunks)
    assert kb_session.chunk_rows(cid, vertical)[0]["chunk_text"] == text


def test_configuration_writes_over_32k(kb_session: KbSession):
    """queue_groups grow by one vertical per document per queue; the config write must survive > 32 KB."""
    cfg = corpus_config()
    cfg["queue_groups"]["Bulk"] = {"label": "Bulk", "verticals": [f"kbdoc-bulk-{i:05d}" for i in range(3000)]}
    assert len(json.dumps(cfg)) > 32767
    cid = kb_session.seed_corpus(cfg, as_oson=True)  # seeded natively, so only KbStore's own write is probed
    kb = kb_session.store()
    vertical = f"kbdoc-it-{RUN}-9"
    kb.set_queues(corpus_id_hex=cid, vertical=vertical, queue_keys=["Bulk", "Card Support"])
    after = kb.get_corpus_config(cid)
    assert after["queue_groups"]["Bulk"]["verticals"][-1] == vertical
    assert len(after["queue_groups"]["Bulk"]["verticals"]) == 3001
    assert kb.unpublish(corpus_id_hex=cid, vertical=vertical)["removed_from_queues"] == ["Bulk", "Card Support"]


def test_chunk_counts_and_ping(kb_session: KbSession):
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    a, b = f"kbdoc-it-{RUN}-10", f"kbdoc-it-{RUN}-11"
    publish(kb, cid, a, ["Card Support"], prepared_chunks(a, 3))
    publish(kb, cid, b, ["HALAN"], prepared_chunks(b, 1))
    assert kb.chunk_counts(cid, [a, b, "kbdoc-it-missing", a]) == {a: 3, b: 1, "kbdoc-it-missing": 0}
    assert kb.chunk_counts(cid, []) == {}
    assert isinstance(kb.ping(), int)


# ---- end to end: real extraction + chunking, fake embedder, DI_TEST publish --------------------------------


class _FakeEmbedder:
    """1536-dim HTTP-style embedder; deterministic, short floats, no network."""

    dimension = DIM

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str], conn: Any = None) -> EmbedBatchResult:
        assert conn is None  # HTTP embedders hold no DB connection
        self.calls += 1
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([round(digest[i % 32] / 255.0 - 0.5, 4) or 0.0001 for i in range(DIM)])
        return EmbedBatchResult(
            vectors=vectors,
            api_total_tokens=sum(len(t) // 4 for t in texts),
            estimated_tokens=sum(max(1, len(t) // 4) for t in texts),
        )


class _MarkerRepo(KbRepo):
    """KbRepo whose only account is this run's synthetic one (AIVA_accounts is only read here)."""

    def __init__(self, db: Database, corpus_id: str) -> None:
        super().__init__(db)
        self._corpus_id = corpus_id

    async def get_account(self, account_id: int) -> dict[str, Any] | None:
        if int(account_id) == MARKER_ACCOUNT:
            return {"id": MARKER_ACCOUNT, "name": f"DI IT {RUN}", "corpus_id": self._corpus_id}
        return None


@pytest.mark.asyncio
async def test_end_to_end_import_of_a_generated_docx(tx_db: TxDatabase, kb_session: KbSession, tmp_path: Path):
    from backend.doc_intel import extraction

    available, why = extraction.extraction_available()
    if not available:
        pytest.skip(f"document-extractor is not usable here: {why}")
    cid = kb_session.seed_corpus(corpus_config())
    kb = kb_session.store()
    repo = _MarkerRepo(tx_db, cid)
    settings = DocIntelSettings(
        _env_file=None, storage_dir=str(tmp_path / "storage"), audit_enabled=False, error_log_enabled=False,
        extraction_timeout_seconds=180,
    )
    side_writes: list[dict[str, Any]] = []

    async def record(**kw: Any) -> None:  # must never be called: nothing outside the doc-intel tables
        side_writes.append(kw)

    embedder = _FakeEmbedder()
    service = KbImportService(
        db=None, repo=repo, kb=kb, settings=settings, embedder_factory=lambda cfg: embedder,
        audit=record, error_log=record, default_price_per_million=0.02,
    )
    user = make_user("SUPER_ADMIN", user_id=MARKER_ACCOUNT)
    upload = UploadFile(file=io.BytesIO(fx.make_docx()), filename="IT دليل العملاء.docx")
    out = await service.upload(user, MARKER_ACCOUNT, ["Card Support"], [upload])
    assert (out.accepted, out.rejected) == (1, 0)
    doc_id = out.documents[0].id
    assert out.documents[0].status == DOC_QUEUED and out.documents[0].queue_labels == ["Card Support"]

    await force_processing(tx_db, doc_id)
    assert await service.process_document(await repo.get_document(doc_id)) == DOC_PUBLISHED

    doc = await service.get_document_out(doc_id)
    assert [s.status for s in doc.stages] == [STAGE_COMPLETED] * 5, doc.stages
    assert doc.status == DOC_PUBLISHED and doc.chunk_count and doc.tokens_used and doc.cost_usd is not None
    assert doc.page_count == 1 and embedder.calls >= 1
    rows = kb_session.chunk_rows(cid, doc.vertical)
    assert len(rows) == doc.chunk_count
    assert all(r["payload_json"]["source"] == KB_PAYLOAD_SOURCE and r["payload_json"]["doc_id"] == doc_id for r in rows)
    assert rows[0]["chunk_text"].startswith("[Document: IT دليل العملاء.docx")
    assert fx.ARABIC_SENTENCE in "\n".join(r["chunk_text"] for r in rows)
    assert queues_with_vertical(kb.get_corpus_config(cid), doc.vertical) == ["Card Support"]

    # The health integrity check sees the published document as intact.
    published = [r for r in await repo.published_documents() if int(r["id"]) == doc_id]
    deps = HealthDeps(db=None, kb=kb, kb_repo=repo, health_repo=None, settings=settings)
    assert await _integrity_problems(deps, published) == []

    gone = await service.unpublish(user, doc_id)
    assert gone.status == "UNPUBLISHED" and kb_session.chunk_rows(cid, doc.vertical) == []
    assert queues_with_vertical(kb.get_corpus_config(cid), doc.vertical) == []
    assert side_writes == []


# ---- isolation check (keep last) -------------------------------------------------------------------


def test_zz_this_run_left_no_rows_behind(oracle: dict[str, Any], capsys: pytest.CaptureFixture[str]):
    """Counted from a fresh session, which only sees committed data."""
    corpus_binds = {f"c{i}": bytes.fromhex(c) for i, c in enumerate(USED_CORPORA)}
    in_list = ", ".join(f":{k}" for k in corpus_binds) or "NULL"
    queries = {
        "AIVA_KB_DOCUMENTS": (f"SELECT COUNT(*) FROM {T} WHERE account_id = :m OR uploaded_by = :m", {"m": MARKER_ACCOUNT}),
        "AIVA_HEALTH_CHECKS": ("SELECT COUNT(*) FROM AIVA_health_checks WHERE component_key LIKE :p", {"p": f"{HEALTH_KEY_PREFIX}%"}),
        "AIVA_HEALTH_CHECK_EVENTS": (
            "SELECT COUNT(*) FROM AIVA_health_check_events WHERE component_key LIKE :p", {"p": f"{HEALTH_KEY_PREFIX}%"},
        ),
        "DI_TEST_KB_CORPUS": (f"SELECT COUNT(*) FROM DI_TEST_KB_CORPUS WHERE corpus_id IN ({in_list})", corpus_binds),
        "DI_TEST_KB_CHUNK": (f"SELECT COUNT(*) FROM DI_TEST_KB_CHUNK WHERE corpus_id IN ({in_list})", corpus_binds),
    }
    conn = oracledb.connect(**oracle)
    try:
        counts = {}
        with conn.cursor() as cur:
            for table, (sql, binds) in queries.items():
                cur.execute(sql, binds)
                counts[table] = int(cur.fetchone()[0])
    finally:
        conn.close()
    with capsys.disabled():
        print(
            f"\n[doc-intel IT] run {RUN}: account_id {MARKER_ACCOUNT}, {len(USED_CORPORA)} DI_TEST corpora, "
            f"health keys {HEALTH_KEY_PREFIX}-*; rows left: {counts}"
        )
    assert counts == dict.fromkeys(queries, 0)
