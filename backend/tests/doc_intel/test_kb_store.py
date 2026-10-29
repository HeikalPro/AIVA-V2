"""KbStore SQL against a recording fake connection (no database).

Checks the statement order of each transaction, the bind variables, commit vs rollback,
and the admin-readable failures. The real SQL runs against the isolated
DI_TEST_KB_CORPUS / DI_TEST_KB_CHUNK copies in the integration tests.
"""
from __future__ import annotations

import json
from decimal import Decimal

import oracledb
import pytest

from backend.doc_intel.chunking import build_chunks
from backend.doc_intel.constants import KB_CHUNKER_VERSION
from backend.doc_intel.kb_store import KbStore, KbTables, PublishFailed
from backend.doc_intel.queue_config import queues_with_vertical
from embedding_service.db.repo import deterministic_chunk_id

from .conftest import CORPUS_ID, RecordingKbConnection, corpus_config, make_document

TABLES = KbTables("DI_TEST_KB_CORPUS", "DI_TEST_KB_CHUNK")
V = "kbdoc-7"


def chunks_for(n_pages: int = 2):
    return build_chunks(
        make_document(pages=n_pages),
        document_id=7,
        filename="Card FAQ.pdf",
        queue_keys=["HALAN"],
        max_chars=600,
        overlap=80,
        max_chunks=500,
    )


def store(config=None, *, found: bool = True) -> tuple[KbStore, RecordingKbConnection]:
    conn = RecordingKbConnection()
    if found:
        raw = config if config is not None else corpus_config()
        conn.on(r"SELECT config_json", [(raw,)])
    return KbStore(conn, TABLES), conn


def written_config(conn: RecordingKbConnection) -> dict:
    (sql, params), = [(s, p) for s, p in conn.statements if s.startswith("UPDATE DI_TEST_KB_CORPUS")]
    assert "CAST(:cfg AS JSON)" in sql
    return json.loads(params["cfg"])


@pytest.mark.parametrize("name", ["kb_corpus; DROP TABLE x", "1abc", "", "a" * 129, "kb corpus", "kb-corpus"])
def test_table_names_are_validated(name):
    with pytest.raises(ValueError):
        KbTables(corpus=name)


def test_default_tables_are_the_live_ones():
    assert KbTables() == KbTables("kb_corpus", "kb_chunk")


def test_get_corpus_config_returns_a_plain_dict():
    raw = corpus_config(chunk_max_chars=Decimal("600"))
    kb, conn = store(raw)
    cfg = kb.get_corpus_config(CORPUS_ID)
    assert cfg["chunk_max_chars"] == 600 and isinstance(cfg["chunk_max_chars"], int)
    (sql, params), = conn.statements
    assert sql.startswith("SELECT config_json FROM DI_TEST_KB_CORPUS")
    assert params == {"cid": bytes.fromhex(CORPUS_ID)}


def test_get_corpus_config_accepts_json_text_and_handles_missing_or_bad_ids():
    kb, _ = store(json.dumps(corpus_config()))
    assert kb.get_corpus_config(CORPUS_ID)["custom_setting"] == {"keep": True}
    kb, conn = store(found=False)
    assert kb.get_corpus_config(CORPUS_ID) is None
    assert kb.get_corpus_config("not-hex") is None
    assert len(conn.statements) == 1  # the bad id never reached the database


def test_publish_is_one_transaction_in_the_right_order():
    kb, conn = store()
    chunks = chunks_for()
    vectors = [[0.5] * 64 for _ in chunks]
    summary = kb.publish(
        corpus_id_hex=CORPUS_ID,
        vertical=V,
        queue_keys=["HALAN", "Cards"],
        chunks=chunks,
        vectors=vectors,
        embedding_model="text-embedding-3-small",
        dimension=64,
    )
    assert conn.verbs() == ["SELECT FOR UPDATE", "DELETE", "INSERT", "UPDATE"]
    assert conn.transactions == ["commit"]
    select_sql = conn.statements[0][0]
    assert "FOR UPDATE WAIT" in select_sql and "DI_TEST_KB_CORPUS" in select_sql

    delete_sql, delete_params = conn.statements[1]
    assert delete_sql == "DELETE FROM DI_TEST_KB_CHUNK WHERE corpus_id = :cid AND external_parent_id = :pid"
    assert delete_params == {"cid": bytes.fromhex(CORPUS_ID), "pid": V}

    insert_sql, rows = conn.statements[2]
    assert insert_sql.startswith("INSERT INTO DI_TEST_KB_CHUNK")
    assert "VECTOR(:emb, 64, FLOAT32)" in insert_sql and "CAST(:payload AS JSON)" in insert_sql
    assert len(rows) == len(chunks)
    first = rows[0]
    assert first["chunk_id"] == deterministic_chunk_id(bytes.fromhex(CORPUS_ID), V, 0, KB_CHUNKER_VERSION)
    assert first["chunker_version"] == KB_CHUNKER_VERSION and first["pid"] == V
    assert first["chunk_text"] == chunks[0].text and first["content_hash"] == chunks[0].content_hash
    assert json.loads(first["payload"])["vertical"] == V
    assert len(json.loads(first["emb"])) == 64

    cfg = written_config(conn)
    assert queues_with_vertical(cfg, V) == ["Cards", "HALAN"]
    assert cfg["custom_setting"] == {"keep": True} and cfg["queue_groups"]["HALAN"]["ivr_hint"] == "press 1"
    assert summary["chunks_written"] == len(chunks)
    assert summary["queues"]["HALAN"]["after"] == ["CF", "Pay", V]
    assert summary["queues"]["HALAN"]["before"] == ["CF", "Pay"]
    assert summary["materialized_default_queue_groups"] is False


def test_publish_writes_large_documents_in_batches():
    kb, conn = store()
    chunks = chunks_for(n_pages=160)
    assert len(chunks) > 200
    kb.publish(
        corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["HALAN"], chunks=chunks,
        vectors=[[0.1] * 64 for _ in chunks], embedding_model="m", dimension=64,
    )
    inserts = [rows for sql, rows in conn.statements if sql.startswith("INSERT")]
    assert len(inserts) > 1 and sum(len(r) for r in inserts) == len(chunks)
    assert conn.transactions == ["commit"]


def test_publish_materializes_default_queue_groups():
    kb, conn = store({"adapter": "halan_records_v1", "embedder": {"dimension": Decimal("64")}})
    chunks = chunks_for()
    summary = kb.publish(
        corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["Gomla"], chunks=chunks,
        vectors=[[0.1] * 64 for _ in chunks], embedding_model="m", dimension=64,
    )
    cfg = written_config(conn)
    assert cfg["queue_groups"]["Gomla"]["verticals"] == ["Gomla", V]
    assert cfg["embedder"]["dimension"] == 64
    assert summary["materialized_default_queue_groups"] is True


def test_publish_missing_corpus_rolls_back():
    kb, conn = store(found=False)
    with pytest.raises(PublishFailed) as info:
        kb.publish(corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["HALAN"], chunks=[], vectors=[], embedding_model="m", dimension=64)
    assert info.value.reason == "Knowledge base corpus not found"
    assert info.value.code == "corpus_not_found" and not info.value.is_database_error
    assert conn.verbs() == ["SELECT FOR UPDATE"]
    assert conn.transactions == ["rollback"]


def test_publish_unknown_queue_rolls_back_before_touching_chunks():
    kb, conn = store()
    chunks = chunks_for()
    with pytest.raises(PublishFailed) as info:
        kb.publish(
            corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["Card Support"], chunks=chunks,
            vectors=[[0.1] * 64 for _ in chunks], embedding_model="m", dimension=64,
        )
    assert info.value.reason == "Queue 'Card Support' no longer exists in this knowledge base"
    assert conn.verbs() == ["SELECT FOR UPDATE"] and conn.transactions == ["rollback"]


@pytest.mark.parametrize(
    ("error", "reason", "code"),
    [
        ("ORA-51803: Vector dimension count must match\nDetails", "Knowledge base database error: ORA-51803: Vector dimension count must match", "database"),
        ("ORA-00257: Archiver error. Connect AS SYSDBA only", "Knowledge base database error: the database host disk is full (ORA-00257 archiver error)", "database"),
        ("ORA-30006: resource busy; acquire with WAIT timeout expired", "The knowledge base configuration is locked by another operation — try again in a minute", "locked"),
    ],
)
def test_publish_database_errors_become_readable_and_roll_back(error, reason, code):
    kb, conn = store()
    conn.raise_on(r"^\s*INSERT", oracledb.DatabaseError(error))
    chunks = chunks_for()
    with pytest.raises(PublishFailed) as info:
        kb.publish(
            corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["HALAN"], chunks=chunks,
            vectors=[[0.1] * 64 for _ in chunks], embedding_model="m", dimension=64,
        )
    assert info.value.reason == reason and info.value.code == code and info.value.is_database_error
    assert conn.transactions == ["rollback"]


def test_publish_rejects_mismatched_vectors():
    kb, conn = store()
    chunks = chunks_for()
    with pytest.raises(PublishFailed):
        kb.publish(corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["HALAN"], chunks=chunks, vectors=[], embedding_model="m", dimension=64)
    assert conn.statements == []


def test_unpublish_removes_the_vertical_first_then_the_chunks():
    cfg = corpus_config()
    cfg["queue_groups"]["HALAN"]["verticals"].append(V)
    cfg["queue_groups"]["Gomla"]["verticals"].append(V)
    kb, conn = store(cfg)
    conn.rowcounts[r"^\s*DELETE"] = 5
    summary = kb.unpublish(corpus_id_hex=CORPUS_ID, vertical=V)
    assert conn.verbs() == ["SELECT FOR UPDATE", "UPDATE", "DELETE"]
    assert conn.transactions == ["commit"]
    assert queues_with_vertical(written_config(conn), V) == []
    assert summary == {"corpus_found": True, "removed_from_queues": ["Gomla", "HALAN"], "chunks_deleted": 5}


def test_unpublish_is_idempotent_and_skips_needless_config_writes():
    kb, conn = store()
    conn.rowcounts[r"^\s*DELETE"] = 0
    summary = kb.unpublish(corpus_id_hex=CORPUS_ID, vertical=V)
    assert conn.verbs() == ["SELECT FOR UPDATE", "DELETE"]
    assert summary == {"corpus_found": True, "removed_from_queues": [], "chunks_deleted": 0}


def test_unpublish_database_error():
    kb, conn = store()
    conn.raise_on(r"^\s*DELETE", oracledb.DatabaseError("DPY-6005: cannot connect to database"))
    with pytest.raises(PublishFailed) as info:
        kb.unpublish(corpus_id_hex=CORPUS_ID, vertical=V)
    assert info.value.reason == "Knowledge base database error: DPY-6005: cannot connect to database"
    assert conn.transactions == ["rollback"]


def test_set_queues_changes_configuration_only():
    cfg = corpus_config()
    cfg["queue_groups"]["HALAN"]["verticals"].append(V)
    kb, conn = store(cfg)
    kb.set_queues(corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["Gomla"])
    assert conn.verbs() == ["SELECT FOR UPDATE", "UPDATE"]
    assert queues_with_vertical(written_config(conn), V) == ["Gomla"]


def test_set_queues_unknown_queue():
    kb, conn = store()
    with pytest.raises(PublishFailed) as info:
        kb.set_queues(corpus_id_hex=CORPUS_ID, vertical=V, queue_keys=["Nope"])
    assert info.value.code == "unknown_queue" and conn.transactions == ["rollback"]


def test_chunk_counts():
    kb, conn = store()
    conn.on(r"GROUP BY", [("kbdoc-1", 4), ("kbdoc-3", 2)])
    counts = kb.chunk_counts(CORPUS_ID, ["kbdoc-1", "kbdoc-2", "kbdoc-3", "kbdoc-1"])
    assert counts == {"kbdoc-1": 4, "kbdoc-2": 0, "kbdoc-3": 2}
    sql, params = conn.statements[-1]
    assert "external_parent_id IN (:p0, :p1, :p2)" in sql
    assert params["cid"] == bytes.fromhex(CORPUS_ID)


def test_ping():
    kb, conn = store()
    assert isinstance(kb.ping(), int)
    assert conn.statements[-1][0] == "SELECT 1 FROM dual"
