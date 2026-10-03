"""KbRepo SQL (recorded, never executed) and the pure stage/row helpers.

Every statement goes through ``check_binds``: python-oracledb rejects both missing and
unused named binds, so a mismatch here would be a production failure.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from backend.doc_intel.constants import KB_STAGES
from backend.doc_intel.kb_repo import (
    INTERRUPTED_REASON,
    KbRepo,
    StageChange,
    apply_stage_change,
    interrupted_stage,
    normalize_corpus_id,
    row_to_out,
    stage_column,
)
from backend.doc_intel.textutil import utc_now

from .conftest import CORPUS_ID, RecordingDatabase

def processing_row(**over):
    row = {"status": "PROCESSING", "stage_details": "{}", "updated_at": utc_now().isoformat(), "worker_id": "w1"}
    row.update(over)
    return row


@pytest.mark.asyncio
async def test_insert_sets_the_vertical_in_the_same_transaction():
    db = RecordingDatabase()
    repo = KbRepo(db)
    doc_id = await repo.insert_document(
        batch_id="a" * 32,
        account_id=3,
        corpus_id=CORPUS_ID,
        queue_keys=["HALAN", "Gomla"],
        filename="ملف " * 400 + ".pdf",  # > 1024 bytes of Arabic
        status="QUEUED",
        stage_statuses={"upload": "COMPLETED"},
        storage_dir="/data/doc_intel/kb/x",
        uploaded_by=7,
        stage_details={"upload": {"started_at": "2026-09-24T10:00:00Z"}},
    )
    (insert_sql, params), (update_sql, update_params) = db.statements
    assert "RETURNING id INTO :out_id" in insert_sql
    assert params["upload_status"] == "COMPLETED" and params["extraction_status"] == "PENDING"
    assert json.loads(params["queue_keys"]) == ["HALAN", "Gomla"]
    assert len(params["filename"].encode("utf-8")) <= 1024
    assert update_sql == "UPDATE AIVA_kb_documents SET vertical = :vertical WHERE id = :id"
    assert update_params == {"vertical": f"kbdoc-{doc_id}", "id": doc_id}
    assert db.commits == 1


@pytest.mark.asyncio
async def test_claim_is_a_conditional_update():
    db = RecordingDatabase()
    db.on(r"SELECT id FROM AIVA_kb_documents WHERE status = 'QUEUED'", {"id": 5})
    db.on(r"WHERE d\.id = :id", {"id": 5, "status": "PROCESSING"})
    repo = KbRepo(db)
    row = await repo.claim_next("host:1:abcd")
    assert row == {"id": 5, "status": "PROCESSING"}
    (update_sql, params), = db.sql_matching(r"^UPDATE")
    assert "WHERE id = :id AND status = 'QUEUED'" in update_sql
    assert "attempts = attempts + 1" in update_sql
    assert params["worker_id"] == "host:1:abcd" and params["id"] == 5


@pytest.mark.asyncio
async def test_claim_lost_race_is_not_a_claim():
    db = RecordingDatabase()
    db.on(r"status = 'QUEUED' ORDER BY", {"id": 5})
    db.rowcounts = [0, 0, 0]
    assert await KbRepo(db).claim_next("w") is None
    assert len(db.sql_matching(r"^UPDATE")) == 3  # tried a few times, never claimed


@pytest.mark.asyncio
async def test_claim_with_nothing_queued():
    db = RecordingDatabase()
    assert await KbRepo(db).claim_next("w") is None
    assert db.sql_matching(r"^UPDATE") == []


@pytest.mark.asyncio
async def test_set_stage_running_then_failed():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row())
    repo = KbRepo(db)
    assert await repo.set_stage(9, "extraction", "RUNNING") is True
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert "extraction_status = :stage_0" in sql and params["stage_0"] == "RUNNING"
    assert "started_at" in json.loads(params["stage_details"])["extraction"]

    reason = "ف" * 3000  # 6000 bytes: must be cut to fit VARCHAR2(4000)
    assert await repo.set_stage(9, "extraction", "FAILED", error=reason) is True
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert params["col_failed_stage"] == "extraction"
    assert len(params["col_error_message"].encode("utf-8")) <= 4000
    assert len(json.loads(params["stage_details"])["extraction"]["error"].encode("utf-8")) <= 4000


@pytest.mark.asyncio
async def test_updates_are_refused_once_the_row_left_processing():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row(status="FAILED"))
    repo = KbRepo(db)
    assert await repo.set_stage(9, "chunking", "RUNNING") is False
    assert await repo.finish(9, status="PUBLISHED") is False
    assert db.sql_matching(r"^UPDATE") == []


@pytest.mark.asyncio
async def test_finish_published_completes_the_stage_and_stamps_published_at():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row(stage_details=json.dumps({"publishing": {"started_at": "2026-09-24T10:00:00Z"}})))
    await KbRepo(db).finish(9, status="PUBLISHED", metrics={"chunks_written": 12}, chunk_count=12, tokens_used=900, cost_usd=0.000018)
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert "published_at = :now" in sql and "finished_at = :now" in sql
    assert params["col_status"] == "PUBLISHED" and params["col_chunk_count"] == 12
    assert "col_page_count" not in params  # unknown values never overwrite earlier ones
    details = json.loads(params["stage_details"])["publishing"]
    assert details["metrics"] == {"chunks_written": 12} and "seconds" in details and "finished_at" in details


@pytest.mark.asyncio
async def test_recover_stale_fails_the_running_stage():
    db = RecordingDatabase()
    old = (utc_now() - timedelta(minutes=30)).isoformat()
    db.on(
        r"status = 'PROCESSING' AND updated_at < :threshold",
        [{"id": 4, "upload_status": "COMPLETED", "extraction_status": "COMPLETED", "chunking_status": "RUNNING",
          "embedding_status": "PENDING", "publishing_status": "PENDING"}],
    )
    db.on(r"FOR UPDATE", processing_row(updated_at=old))
    recovered = await KbRepo(db).recover_stale(utc_now() - timedelta(minutes=5))
    assert recovered == [4]
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert params["stage_0"] == "FAILED" and "chunking_status = :stage_0" in sql
    assert params["col_status"] == "FAILED" and params["col_error_message"] == INTERRUPTED_REASON
    assert json.loads(params["stage_details"])["chunking"]["error"] == INTERRUPTED_REASON


@pytest.mark.asyncio
async def test_recover_stale_skips_a_row_touched_meanwhile():
    db = RecordingDatabase()
    db.on(r"updated_at < :threshold", [{"id": 4, "chunking_status": "RUNNING"}])
    db.on(r"FOR UPDATE", processing_row(updated_at=utc_now().isoformat()))  # heartbeat just arrived
    assert await KbRepo(db).recover_stale(utc_now() - timedelta(minutes=5)) == []
    assert db.sql_matching(r"^UPDATE") == []


@pytest.mark.asyncio
async def test_release_interrupted_only_for_this_worker():
    db = RecordingDatabase()
    db.on(r"worker_id = :worker_id", {"id": 4, "embedding_status": "RUNNING"})
    db.on(r"FOR UPDATE", processing_row(worker_id="other"))
    assert await KbRepo(db).release_interrupted(4, "me") is False
    db.on(r"FOR UPDATE", processing_row(worker_id="me"))
    assert await KbRepo(db).release_interrupted(4, "me") is True


@pytest.mark.asyncio
async def test_reset_for_retry_from_chunking():
    db = RecordingDatabase()
    details = {s: {"started_at": "2026-09-24T10:00:00Z", "finished_at": "2026-09-24T10:00:01Z"} for s in ("upload", "extraction")}
    details["chunking"] = {"error": "boom"}
    db.on(r"FOR UPDATE", processing_row(status="FAILED", stage_details=json.dumps(details)))
    assert await KbRepo(db).reset_for_retry(4, "chunking") is True
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert [params[f"stage_{i}"] for i in range(3)] == ["PENDING"] * 3
    assert "chunking_status = :stage_0" in sql and "publishing_status = :stage_2" in sql
    assert params["col_status"] == "QUEUED" and params["col_failed_stage"] is None
    assert set(json.loads(params["stage_details"])) == {"upload", "extraction"}


@pytest.mark.asyncio
async def test_reset_for_retry_requires_failed():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row(status="PUBLISHED"))
    assert await KbRepo(db).reset_for_retry(4, "extraction") is False


@pytest.mark.asyncio
async def test_republish_mark_unpublished_and_queue_updates_are_conditional():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row(status="QUEUED"))
    repo = KbRepo(db)
    assert await repo.reset_for_republish(4) is False
    assert await repo.mark_unpublished(4) is False
    assert await repo.update_queue_keys(4, ["HALAN"], statuses=("QUEUED", "FAILED")) is True
    _, params = db.sql_matching(r"^UPDATE")[-1]
    assert json.loads(params["col_queue_keys"]) == ["HALAN"]


@pytest.mark.asyncio
async def test_find_active_duplicate_and_touch():
    db = RecordingDatabase()
    db.on(r"sha256 = :sha256", {"id": 12})
    repo = KbRepo(db)
    assert await repo.find_active_duplicate(CORPUS_ID, "f" * 64) == 12
    sql, _ = db.statements[-1]
    assert "status IN ('QUEUED', 'PROCESSING', 'PUBLISHED')" in sql
    await repo.touch(12, "w1")
    sql, params = db.statements[-1]
    assert "AND worker_id = :worker_id" in sql and params["worker_id"] == "w1"


@pytest.mark.asyncio
async def test_list_documents_filters_and_total():
    db = RecordingDatabase()
    db.on(r"COUNT\(\*\) AS total", {"total": 7})
    rows, total = await KbRepo(db).list_documents(account_id=3, status="FAILED", batch_id="b" * 32, limit=10, offset=20)
    assert total == 7 and rows == []
    select_sql, params = db.statements[0]
    assert "d.account_id = :account_id AND d.status = :status AND d.batch_id = :batch_id" in select_sql
    assert params["offset"] == 20 and params["limit"] == 10
    assert "LEFT JOIN AIVA_accounts" in select_sql and "LEFT JOIN AIVA_users" in select_sql


@pytest.mark.asyncio
async def test_monitoring_queries_are_well_formed():
    db = RecordingDatabase()
    repo = KbRepo(db)
    await repo.status_counts()
    await repo.stuck_documents(utc_now())
    await repo.failed_since(utc_now())
    await repo.published_documents()
    await repo.failures(7)
    await repo.activity(50)
    await repo.queue_positions()
    await repo.distinct_account_corpora()
    assert len(db.statements) == 9  # every statement passed the bind check


@pytest.mark.asyncio
async def test_unknown_columns_and_stages_are_rejected():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", processing_row())
    with pytest.raises(ValueError):
        await KbRepo(db)._update(1, stages=[], columns={"status; DROP TABLE x": "y"})
    with pytest.raises(ValueError):
        stage_column("upload_status OR 1=1")


def test_apply_stage_change_rules():
    now = utc_now()
    details = apply_stage_change({}, StageChange("embedding", "RUNNING"), now=now - timedelta(seconds=3))
    done = apply_stage_change(details, StageChange("embedding", "COMPLETED", metrics={"batches": 2}), now=now)
    assert done["embedding"]["seconds"] == pytest.approx(3, abs=0.01)
    assert done["embedding"]["metrics"] == {"batches": 2}
    failed = apply_stage_change(done, StageChange("embedding", "FAILED", error="401"), now=now)
    assert failed["embedding"]["error"] == "401"
    rerun = apply_stage_change(failed, StageChange("embedding", "RUNNING"), now=now)
    assert set(rerun["embedding"]) == {"started_at"}  # a new run starts clean
    assert "embedding" not in apply_stage_change(failed, StageChange("embedding", "PENDING"), now=now)
    with pytest.raises(ValueError):
        apply_stage_change({}, StageChange("embedding", "DONE"), now=now)


def test_interrupted_stage_choice():
    assert interrupted_stage({"upload_status": "COMPLETED", "extraction_status": "RUNNING"}) == "extraction"
    assert interrupted_stage({"upload_status": "COMPLETED", "extraction_status": "COMPLETED", "chunking_status": "PENDING"}) == "chunking"
    assert interrupted_stage({s + "_status": "COMPLETED" for s in KB_STAGES}) == "publishing"


def test_normalize_corpus_id():
    assert normalize_corpus_id("091B8D61-C546-45EF-86DF-0D78E0B9AE0C") == CORPUS_ID
    assert normalize_corpus_id("xyz") is None and normalize_corpus_id(None) is None


def test_row_to_out_shapes_the_api_model():
    row = {
        "id": 12, "batch_id": "b" * 32, "account_id": 3, "account_name": "Hallan", "organization_name": "GoChat247",
        "corpus_id": CORPUS_ID, "queue_keys": '["HALAN", "Cards"]', "vertical": "kbdoc-12", "filename": "a.pdf",
        "content_type": "application/pdf", "size_bytes": 1234, "sha256": "f" * 64, "status": "FAILED",
        "upload_status": "COMPLETED", "extraction_status": "COMPLETED", "chunking_status": "COMPLETED",
        "embedding_status": "FAILED", "publishing_status": "PENDING", "failed_stage": "embedding",
        "error_message": "Embedding provider rejected the API key (401)",
        "stage_details": json.dumps({"upload": {"started_at": "2026-09-24T10:00:00Z", "finished_at": "2026-09-24T10:00:01Z"},
                                     "embedding": {"started_at": "2026-09-24T10:01:00Z"}}),
        "warnings_json": json.dumps([{"code": "low_text", "message": "Page 2 has little text", "page": 2}, {"bad": 1}]),
        "page_count": 2, "chunk_count": 5, "tokens_used": None, "cost_usd": None, "attempts": 1,
        "uploaded_by": 7, "uploaded_by_email": "sa@example.test",
        "created_at": "2026-09-24T10:00:00.123456", "updated_at": "2026-09-24T10:01:05", "started_at": None,
        "finished_at": None, "published_at": None,
    }
    out = row_to_out(row, queue_labels=["Halan", "Card Support"], queue_position=3)
    assert [s.name for s in out.stages] == list(KB_STAGES)
    embedding = out.stages[3]
    assert embedding.status == "FAILED" and embedding.error == "Embedding provider rejected the API key (401)"
    assert out.stages[0].finished_at == "2026-09-24T10:00:01Z"
    assert out.created_at == "2026-09-24T10:00:00.123456Z"
    assert out.queue_labels == ["Halan", "Card Support"]
    assert out.queue_position is None  # only QUEUED documents have a position
    assert [w.code for w in out.warnings] == ["low_text"]
    assert out.failed_stage == "embedding"
    queued = row_to_out({**row, "status": "QUEUED"}, queue_labels=None, queue_position=3)
    assert queued.queue_position == 3 and queued.queue_labels == ["HALAN", "Cards"]
