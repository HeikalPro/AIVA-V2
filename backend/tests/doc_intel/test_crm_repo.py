"""CrmRepo SQL (recorded, never executed), the row -> API mappers and the entity helpers.

Every statement goes through conftest's ``check_binds``: python-oracledb rejects both missing
and unused named binds, so a mismatch here would be a production failure.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import oracledb
import pytest

from backend.doc_intel.crm_repo import (
    INTERRUPTED_FILE_REASON,
    INTERRUPTED_RUN_REASON,
    SHUTDOWN_RUN_REASON,
    CrmRepo,
    EntityRow,
    RunAlreadyActive,
    apply_file_stage_change,
    build_entity_rows,
    entity_display_value,
    entity_match_key,
    entity_to_out,
    file_stage_column,
    file_to_out,
    running_file_stage,
    schema_hints,
    source_to_out,
)
from backend.doc_intel.kb_repo import StageChange
from backend.doc_intel.textutil import utc_now

from ._crm_fakes import CLIENT_ID, SECRET, TENANT_ID, FakeBox, remote
from .conftest import RecordingDatabase

CRM_STAGES = ("download", "extraction", "intelligence", "entities", "persist")
ARABIC = "عقد توريد مع شركة النيل للتجارة "


class ErrorDatabase(RecordingDatabase):
    """RecordingDatabase whose execute() raises ``error`` for SQL matching ``pattern``."""

    def __init__(self, pattern: str, error: Exception) -> None:
        super().__init__()
        self._pattern, self._error = pattern, error

    async def execute(self, sql, params=None, *, conn=None, return_id=False):
        result = await super().execute(sql, params, conn=conn, return_id=return_id)
        if self._pattern in sql:
            raise self._error
        return result


def file_row(**over):
    row = {"status": "PROCESSING", "state": "ACTIVE", "stage_details": "{}"}
    row.update(over)
    return row


def encrypted_source(box: FakeBox, **over):
    row = {
        "id": 7, "name": "Sales", "account_id": 3, "account_name": "Hallan", "provider": "microsoft_graph",
        "tenant_id_enc": box.encrypt(TENANT_ID), "client_id_enc": box.encrypt(CLIENT_ID), "client_secret_set": 1,
        "client_secret_hint": "…" + SECRET[-4:], "secret_updated_at": "2026-09-25T10:00:00", "site_url": "https://x.sharepoint.com/sites/s",
        "drive_name": None, "folder_path": "/CRM", "recursive": 1, "file_extensions": ".pdf,.docx", "use_intelligence": 0,
        "sync_enabled": 1, "sync_interval_days": 7, "sync_hour": 3, "next_sync_at": "2026-10-01T00:00:00",
        "last_sync_at": None, "last_sync_status": None, "last_sync_error": None, "last_success_at": None,
        "status": "ACTIVE", "created_at": "2026-09-25T09:00:00", "updated_at": "2026-09-25T09:30:00.5",
    }
    row.update(over)
    return row


# ---- sources ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_source_binds_only_ciphertext():
    db = RecordingDatabase()
    source_id = await CrmRepo(db).insert_source(
        name=ARABIC * 20, account_id=3, tenant_id_enc="fernet:v1:t", client_id_enc="fernet:v1:c",
        client_secret_enc="fernet:v1:s", client_secret_hint="…abcd", secret_updated_at=utc_now(),
        site_url="https://contoso.sharepoint.com/sites/Sales", drive_name=None, folder_path="/CRM", recursive=True,
        file_extensions=[".pdf", ".docx"], sync_enabled=False, sync_interval_days=14, sync_hour=2, next_sync_at=None,
        created_by=101,
    )
    ((sql, params),) = db.statements
    assert source_id == 42 and "RETURNING id INTO :out_id" in sql and "'ACTIVE'" in sql
    assert params["client_secret_enc"] == "fernet:v1:s" and params["file_extensions"] == ".pdf,.docx"
    assert (params["recursive"], params["sync_enabled"], params["use_intelligence"]) == (1, 0, 0)
    assert len(params["name"].encode("utf-8")) <= 512  # Arabic name cut to the column's bytes


@pytest.mark.asyncio
async def test_listing_queries_never_select_the_secret_ciphertext():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    await repo.get_source(7)
    await repo.list_sources()
    await repo.get_source(7, include_deleted=True)
    for sql, _ in db.statements:
        assert "CASE WHEN s.client_secret_enc IS NULL THEN 0 ELSE 1 END AS client_secret_set" in sql
        assert sql.count("client_secret_enc") == 1
    assert "s.status <> 'DELETED'" in db.statements[0][0] and "s.status <> 'DELETED'" in db.statements[1][0]
    assert "s.status <> 'DELETED'" not in db.statements[2][0]
    await repo.get_credentials(7)
    sql, params = db.statements[-1]
    assert "client_secret_enc" in sql and "status <> 'DELETED'" in sql and params == {"id": 7}


@pytest.mark.asyncio
async def test_update_source_whitelists_columns_and_skips_deleted_rows():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    assert await repo.update_source(7, {"name": "x", "next_sync_at": None}, updated_by=101) is True
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert "WHERE id = :id AND status <> 'DELETED'" in sql and "updated_by = :updated_by" in sql
    assert params["col_name"] == "x" and params["col_next_sync_at"] is None and params["updated_by"] == 101
    db.rowcounts = [0]
    assert await repo.update_source(7, {"name": "y"}, updated_by=1) is False
    with pytest.raises(ValueError):
        await repo.update_source(7, {"last_sync_status; DROP TABLE x": "y"}, updated_by=1)
    with pytest.raises(ValueError):
        await repo.update_source(7, {"created_by": 5}, updated_by=1)


@pytest.mark.asyncio
async def test_set_resolved_is_conditional_on_the_location():
    db = RecordingDatabase()
    target = type("T", (), {"site_id": "s1", "drive_id": "d1", "folder_id": "f1"})()
    assert await CrmRepo(db).set_resolved(7, target, site_url="https://x", drive_name=None, folder_path="/CRM")
    ((sql, params),) = db.statements
    for column in ("site_url", "drive_name", "folder_path"):
        assert f"DECODE({column}, :{column}, 1, 0) = 1" in sql
    assert params["drive_name"] is None and params["folder_id"] == "f1"
    long_id = type("T", (), {"site_id": "s" * 600, "drive_id": "d", "folder_id": "f"})()
    assert await CrmRepo(db).set_resolved(7, long_id, site_url="https://x", drive_name=None, folder_path=None) is False


@pytest.mark.asyncio
async def test_soft_delete_locks_the_source_refuses_active_runs_and_withdraws_the_entities():
    db = RecordingDatabase()
    db.on(r"FROM AIVA_crm_sources WHERE id = :id FOR UPDATE", {"status": "ACTIVE"})
    db.on(r"status IN \('QUEUED', 'RUNNING'\)", {"n": 0})
    db.rowcounts = [1, 3]  # the source row, then three entities withdrawn
    assert await CrmRepo(db).soft_delete_source(7, updated_by=101) == 3
    (lock_sql, _), (active_sql, _), (source_sql, _), (entity_sql, entity_params) = db.statements
    assert "SELECT status FROM AIVA_crm_sources WHERE id = :id FOR UPDATE" in lock_sql
    assert "FROM AIVA_crm_sync_runs WHERE source_id = :id AND status IN ('QUEUED', 'RUNNING')" in active_sql
    assert "status = 'DELETED'" in source_sql and "client_secret_enc = NULL" in source_sql and "sync_enabled = 0" in source_sql
    assert "SET status = 'WITHDRAWN', withdrawn_at = :now" in entity_sql and "WHERE source_id = :id AND status = 'ACTIVE'" in entity_sql
    assert entity_params["id"] == 7 and db.commits == 1

    busy = RecordingDatabase()
    busy.on(r"FOR UPDATE", {"status": "ACTIVE"})
    busy.on(r"status IN \('QUEUED', 'RUNNING'\)", {"n": 1})
    with pytest.raises(RunAlreadyActive):
        await CrmRepo(busy).soft_delete_source(7, updated_by=101)
    assert busy.sql_matching(r"^UPDATE") == [] and busy.rollbacks == 1
    assert await CrmRepo(RecordingDatabase()).soft_delete_source(7, updated_by=101) is None  # no such source
    deleted = RecordingDatabase()
    deleted.on(r"FOR UPDATE", {"status": "DELETED"})
    assert await CrmRepo(deleted).soft_delete_source(7, updated_by=101) is None
    assert deleted.sql_matching(r"^UPDATE") == []


@pytest.mark.asyncio
async def test_schedule_claim_is_optimistic():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    old, now, new = datetime(2026, 9, 26, 0, 0), datetime(2026, 9, 26, 0, 0, 30, 123456), datetime(2026, 10, 10, 0, 0)
    assert await repo.claim_due_source(7, now=now, new=new) is True
    sql, params = db.statements[-1]
    # F24: claimed while still due, never on equality with the value read (a DATE bind drops fractions).
    assert "SET next_sync_at = :new" in sql and "next_sync_at <= :now" in sql and "sync_enabled = 1" in sql
    assert "status = 'ACTIVE'" in sql and "next_sync_at = :old" not in sql and ":old" not in sql
    assert (params["now"], params["new"]) == (now, new)
    db.rowcounts = [0]
    assert await repo.claim_due_source(7, now=now, new=new) is False
    with pytest.raises(ValueError):  # a "new" that is not after "now" would let a second claimer in
        await repo.claim_due_source(7, now=now, new=now)
    assert await repo.restore_next_sync(7, expected=new, value=old) is True
    sql, _ = db.statements[-1]
    assert "WHERE id = :id AND next_sync_at = :expected" in sql
    await repo.due_sources(utc_now())
    sql, _ = db.statements[-1]
    assert "status = 'ACTIVE' AND sync_enabled = 1 AND next_sync_at IS NOT NULL AND next_sync_at <= :now" in sql


# ---- runs ------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_maps_the_active_run_index_to_run_already_active():
    violation = oracledb.IntegrityError(
        "ORA-00001: unique constraint (AI_ASSISTANT.UQ_AIVA_CRM_RUN_ACTIVE) violated on table "
        "AI_ASSISTANT.AIVA_CRM_SYNC_RUNS columns (SYS_NC00019$)"
    )
    with pytest.raises(RunAlreadyActive) as info:
        await CrmRepo(ErrorDatabase("INSERT INTO AIVA_crm_sync_runs", violation)).create_run(7, trigger_type="MANUAL", triggered_by=1)
    assert info.value.source_id == 7 and info.value.__cause__ is None
    other = oracledb.IntegrityError("ORA-00001: unique constraint (AI_ASSISTANT.PK_AIVA_CRM_SYNC_RUNS) violated")
    with pytest.raises(oracledb.IntegrityError):
        await CrmRepo(ErrorDatabase("INSERT INTO AIVA_crm_sync_runs", other)).create_run(7, trigger_type="MANUAL", triggered_by=1)
    db = RecordingDatabase()
    assert await CrmRepo(db).create_run(7, trigger_type="SCHEDULED", triggered_by=None) == 42
    ((sql, params),) = db.statements
    assert "'QUEUED'" in sql and params["trigger_type"] == "SCHEDULED" and params["triggered_by"] is None


@pytest.mark.asyncio
async def test_claim_next_run_is_a_conditional_update():
    db = RecordingDatabase()
    db.on(r"WHERE status = 'QUEUED' ORDER BY created_at", {"id": 5})
    db.on(r"WHERE r\.id = :id", {"id": 5, "status": "RUNNING"})
    run = await CrmRepo(db).claim_next_run("host:1:sync-abcd")
    assert run == {"id": 5, "status": "RUNNING"}
    ((sql, params),) = db.sql_matching(r"^UPDATE")
    assert "WHERE id = :id AND status = 'QUEUED'" in sql and "status = 'RUNNING'" in sql
    assert params["worker_id"] == "host:1:sync-abcd"

    lost = RecordingDatabase()
    lost.on(r"status = 'QUEUED' ORDER BY", {"id": 5})
    lost.rowcounts = [0, 0, 0]
    assert await CrmRepo(lost).claim_next_run("w") is None and len(lost.sql_matching(r"^UPDATE")) == 3
    empty = RecordingDatabase()
    assert await CrmRepo(empty).claim_next_run("w") is None and empty.sql_matching(r"^UPDATE") == []


@pytest.mark.asyncio
async def test_heartbeat_and_progress_are_owned_by_the_worker():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    await repo.touch_run(5, "w1")
    sql, params = db.statements[-1]
    assert "status = 'RUNNING' AND worker_id = :worker_id" in sql and params["worker_id"] == "w1"
    assert await repo.set_run_progress(5, worker_id="w1", counts={"files_seen": 3, "files_failed": 1}, details={"x": 1})
    sql, params = db.statements[-1]
    assert "files_seen = :files_seen" in sql and "files_new" not in sql and json.loads(params["details_json"]) == {"x": 1}
    db.rowcounts = [0]
    assert await repo.set_run_progress(5, worker_id="w1", counts={}) is False


@pytest.mark.asyncio
async def test_finish_run_records_the_result_on_the_source_in_one_transaction():
    db = RecordingDatabase()
    next_at = datetime(2026, 10, 2, 0, 0)
    ok = await CrmRepo(db).finish_run(
        5, worker_id="w1", status="PARTIAL", error_message="ف" * 3000, counts={"files_seen": 3, "files_failed": 1},
        details={"listing_complete": False}, source_id=7, next_sync_at=next_at, schedule_interval_days=7, schedule_hour=3,
    )
    assert ok and db.commits == 1
    (run_sql, run_params), (source_sql, source_params) = db.statements
    assert "WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id" in run_sql
    assert len(run_params["error_message"].encode("utf-8")) <= 4000 and run_params["files_new"] == 0
    assert "last_success_at = CASE WHEN :status = 'COMPLETED' THEN :now ELSE last_success_at END" in source_sql
    assert "sync_interval_days = :interval AND sync_hour = :hour THEN :next_sync_at ELSE next_sync_at END" in source_sql
    assert (source_params["interval"], source_params["hour"], source_params["next_sync_at"]) == (7, 3, next_at)
    assert "WHERE id = :id AND status <> 'DELETED'" in source_sql

    no_schedule = RecordingDatabase()
    await CrmRepo(no_schedule).finish_run(
        5, worker_id="w1", status="FAILED", error_message="x", counts={}, details={}, source_id=7,
        next_sync_at=None, schedule_interval_days=None, schedule_hour=None,
    )
    assert "next_sync_at" not in no_schedule.statements[-1][0]

    lost = RecordingDatabase()
    lost.rowcounts = [0]
    assert await CrmRepo(lost).finish_run(
        5, worker_id="w1", status="COMPLETED", error_message=None, counts={}, details={}, source_id=7,
        next_sync_at=None, schedule_interval_days=None, schedule_hour=None,
    ) is False
    assert len(lost.statements) == 1  # the source is not touched for an abandoned run
    with pytest.raises(ValueError):
        await CrmRepo(RecordingDatabase()).finish_run(
            5, worker_id="w1", status="RUNNING", error_message=None, counts={}, details={}, source_id=7,
            next_sync_at=None, schedule_interval_days=None, schedule_hour=None,
        )


@pytest.mark.asyncio
async def test_recover_stale_runs_fails_the_run_its_file_and_records_it():
    db = RecordingDatabase()
    db.on(r"FROM AIVA_crm_sync_runs WHERE status = 'RUNNING' AND updated_at < :threshold", [{"id": 9, "source_id": 7}])
    db.on(r"WHERE last_run_id = :run_id AND status = 'PROCESSING'", [
        {"id": 31, "download_status": "COMPLETED", "extraction_status": "RUNNING", "intelligence_status": "PENDING",
         "entities_status": "PENDING", "persist_status": "PENDING"},
    ])
    db.on(r"FOR UPDATE", file_row())
    threshold = utc_now() - timedelta(minutes=5)
    assert await CrmRepo(db).recover_stale_runs(threshold) == [9]
    updates = db.sql_matching(r"^UPDATE")
    run_sql, run_params = updates[0]
    assert "AND updated_at < :threshold" in run_sql and run_params["reason"] == INTERRUPTED_RUN_REASON
    file_sql, file_params = updates[1]
    assert "extraction_status = :stage_0" in file_sql and file_params["stage_0"] == "FAILED"
    assert file_params["col_error_message"] == INTERRUPTED_FILE_REASON
    assert json.loads(file_params["stage_details"])["extraction"]["error"] == INTERRUPTED_FILE_REASON
    source_sql, source_params = updates[2]
    assert "AIVA_crm_sources" in source_sql and source_params["status"] == "FAILED" and "next_sync_at" not in source_sql
    assert db.commits == 1  # run + file + source in one transaction

    raced = RecordingDatabase()
    raced.on(r"status = 'RUNNING' AND updated_at < :threshold", [{"id": 9, "source_id": 7}])
    raced.rowcounts = [0]  # the heartbeat arrived meanwhile
    assert await CrmRepo(raced).recover_stale_runs(threshold) == []
    assert len(raced.sql_matching(r"^UPDATE")) == 1


@pytest.mark.asyncio
async def test_release_interrupted_run_is_only_for_this_worker():
    db = RecordingDatabase()
    assert await CrmRepo(db).release_interrupted_run(9, "me") is False  # not found for this worker
    sql, params = db.statements[-1]
    assert "worker_id = :worker_id" in sql and params == {"id": 9, "worker_id": "me"}
    db.on(r"WHERE id = :id AND status = 'RUNNING' AND worker_id = :worker_id", {"id": 9, "source_id": 7})
    assert await CrmRepo(db).release_interrupted_run(9, "me") is True
    run_sql, run_params = db.sql_matching(r"^UPDATE AIVA_crm_sync_runs")[-1]
    assert run_params["reason"] == SHUTDOWN_RUN_REASON and "AND worker_id = :worker_id" in run_sql


# ---- files -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_inserts_an_unknown_file_and_rearms_a_known_one():
    db = RecordingDatabase()
    file_id = await CrmRepo(db).upsert_file(7, remote("a", name=ARABIC * 60 + ".pdf"), run_id=3)
    select_sql, _ = db.statements[0]
    insert_sql, params = db.statements[1]
    assert "source_id = :source_id AND drive_id = :drive_id AND item_id = :item_id FOR UPDATE" in select_sql
    assert file_id == 42 and "RETURNING id INTO :out_id" in insert_sql and "'{}'" in insert_sql
    assert insert_sql.count("'PENDING'") == 6 and len(params["name"].encode("utf-8")) <= 1024
    assert params["modified_at"] == datetime(2026, 9, 1, 10, 0) and params["run_id"] == 3

    known = RecordingDatabase()
    known.on(r"item_id = :item_id FOR UPDATE", {"id": 11})
    known.on(r"WHERE id = :id FOR UPDATE", file_row(status="FAILED", stage_details=json.dumps({"download": {"error": "x"}})))
    assert await CrmRepo(known).upsert_file(7, remote("a", ctag="c2"), run_id=3, reset_attempts=False) == 11
    sql, params = known.sql_matching(r"^UPDATE")[-1]
    assert [params[f"stage_{i}"] for i in range(5)] == ["PENDING"] * 5 and json.loads(params["stage_details"]) == {}
    assert params["col_state"] == "ACTIVE" and params["col_deleted_at"] is None and params["col_ctag"] == "c2"
    assert "attempts" not in sql  # a plain retry keeps the attempt count
    await CrmRepo(known).upsert_file(7, remote("a", ctag="c3"), run_id=4, reset_attempts=True)
    assert known.sql_matching(r"^UPDATE")[-1][1]["col_attempts"] == 0


@pytest.mark.asyncio
async def test_mark_seen_and_deleted_work_in_chunks_and_withdraw_entities():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    await repo.mark_seen(list(range(1, 502)))
    seen = db.sql_matching(r"SET last_seen_at = :now WHERE id IN")
    assert len(seen) == 2 and len(seen[0][1]) == 501 and "updated_at" not in seen[0][0]
    db.statements.clear()
    assert await repo.mark_files_deleted([5, 6, 5]) == 1  # the recording cursor reports rowcount 1
    (file_sql, file_params), (entity_sql, entity_params) = db.statements
    assert "SET state = 'DELETED', deleted_at = :now, updated_at = :now" in file_sql and "AND state = 'ACTIVE'" in file_sql
    assert set(file_params) == {"f0", "f1", "now"}
    assert "SET status = 'WITHDRAWN', withdrawn_at = :now" in entity_sql and "source_file_id IN (:f0, :f1)" in entity_sql
    assert db.commits == 1


@pytest.mark.asyncio
async def test_file_stage_updates_are_conditional_and_merge_the_details():
    db = RecordingDatabase()
    db.on(r"FOR UPDATE", file_row(status="PENDING"))
    repo = CrmRepo(db)
    assert await repo.begin_file(31, 3) is True
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert "attempts = attempts + 1" in sql and params["col_status"] == "PROCESSING" and params["col_last_run_id"] == 3

    db.on(r"FOR UPDATE", file_row())
    assert await repo.set_file_stage(31, "download", "RUNNING")
    assert await repo.set_file_stage(31, "download", "COMPLETED", metrics={"size_bytes": 10})
    reason = "ف" * 3000
    assert await repo.fail_file(31, "intelligence", reason, completed=("extraction",), metrics={"code": "x"})
    sql, params = db.sql_matching(r"^UPDATE")[-1]
    assert "extraction_status = :stage_0" in sql and "intelligence_status = :stage_1" in sql
    assert (params["stage_0"], params["stage_1"]) == ("COMPLETED", "FAILED")
    assert params["col_failed_stage"] == "intelligence" and len(params["col_error_message"].encode("utf-8")) <= 4000
    assert "processed_at = :now" in sql

    db.on(r"FOR UPDATE", file_row(status="FAILED"))
    assert await repo.set_file_stage(31, "download", "RUNNING") is False  # left PROCESSING: refused
    db.on(r"FOR UPDATE", file_row(status="COMPLETED"))
    assert await repo.begin_file(31, 3) is False
    db.on(r"FOR UPDATE", file_row(status="FAILED", state="DELETED"))
    assert await repo.reset_file_for_retry(31) is False
    with pytest.raises(ValueError):
        file_stage_column("persist_status OR 1=1")


@pytest.mark.asyncio
async def test_complete_file_replaces_entities_and_completes_in_one_transaction():
    db = RecordingDatabase()
    db.on(r"SELECT status, state FROM AIVA_crm_source_files WHERE id = :id FOR UPDATE", file_row())
    db.on(r"SELECT status, state, stage_details", file_row())
    rows = build_entity_rows([{"name": "Acme", "_meta": {"entity_type": "organization", "confidence": 0.7}},
                              {"full_name": "Mona", "email": "M@X.io", "_meta": {"entity_type": "contact"}}])
    ok = await CrmRepo(db).complete_file(
        31, source_id=7, account_id=3, entities=rows, result={"big": "x" * 40000}, warnings=[{"code": "w", "message": "m"}],
        is_valid=True, content_sha256="a" * 64, metrics={"entities_written": 2},
    )
    assert ok and db.commits == 1
    verbs = [s.split()[0] for s, _ in db.statements]
    assert verbs == ["SELECT", "UPDATE", "INSERT", "INSERT", "SELECT", "UPDATE"]
    withdraw_sql, _ = db.statements[1]
    assert "SET status = 'WITHDRAWN', withdrawn_at = :now" in withdraw_sql and "source_file_id = :file_id AND status = 'ACTIVE'" in withdraw_sql
    _, insert = db.statements[2]
    assert insert["source_file_id"] == 31 and insert["account_id"] == 3 and insert["entity_type"] == "organization"
    assert json.loads(insert["fields_json"])["_meta"]["entity_type"] == "organization"
    sql, params = db.statements[-1]
    assert params["col_status"] == "COMPLETED" and params["col_entity_count"] == 2 and params["col_is_valid"] == 1
    assert len(params["col_result_json"]) > 32767  # a CLOB bound as one string (plain UPDATE, not MERGE)
    assert json.loads(params["stage_details"])["persist"]["metrics"] == {"withdrawn": 1, "inserted": 2, "entities_written": 2}

    refused = RecordingDatabase()
    refused.on(r"FOR UPDATE", file_row(status="FAILED"))
    assert await CrmRepo(refused).complete_file(
        31, source_id=7, account_id=None, entities=rows, result={}, warnings=[], is_valid=None, content_sha256=None,
    ) is False
    assert [s.split()[0] for s, _ in refused.statements] == ["SELECT"]  # nothing written


@pytest.mark.asyncio
async def test_entity_listing_filters_and_escapes_the_search():
    db = RecordingDatabase()
    db.on(r"COUNT\(\*\) AS total", {"total": 4})
    rows, total = await CrmRepo(db).list_entities(source_id=7, entity_type="contact", status="ACTIVE", q=" 50%_off\\ ", limit=10, offset=20)
    assert total == 4 and rows == []
    sql, params = db.statements[0]
    assert "e.source_id = :source_id AND e.entity_type = :entity_type AND e.status = :status" in sql
    assert "LIKE :q ESCAPE '\\'" in sql and params["q"] == "%50\\%\\_off\\\\%"
    assert "LEFT JOIN AIVA_crm_sources s" in sql and "LEFT JOIN AIVA_crm_source_files f" in sql
    assert (params["offset"], params["limit"]) == (20, 10)


@pytest.mark.asyncio
async def test_counts_group_files_and_entities_per_source():
    db = RecordingDatabase()
    db.on(r"FROM AIVA_crm_source_files", [
        {"source_id": 7, "state": "ACTIVE", "status": "COMPLETED", "n": 5},
        {"source_id": 7, "state": "ACTIVE", "status": "FAILED", "n": 2},
        {"source_id": 7, "state": "DELETED", "status": "COMPLETED", "n": 1},
    ])
    db.on(r"FROM AIVA_crm_entities", [{"source_id": 7, "n": 9}])
    counts = await CrmRepo(db).counts_by_source(7)
    assert counts == {7: {"files_active": 7, "files_failed": 2, "files_deleted": 1, "entities_active": 9}}
    assert all(p == {"source_id": 7} for _, p in db.statements)


@pytest.mark.asyncio
async def test_monitoring_queries_are_well_formed():
    db = RecordingDatabase()
    repo = CrmRepo(db)
    now = utc_now()
    await repo.store_stats()
    await repo.last_runs()
    await repo.persist_failures([1, 2, 3])
    await repo.persist_failures([])
    await repo.stuck_runs(now)
    await repo.failed_files_since(now)
    await repo.failed_runs_since(now)
    await repo.run_activity(50)
    await repo.file_activity(50)
    await repo.active_runs()
    await repo.active_runs(7)
    await repo.list_runs(7)
    await repo.list_files(7, state="ACTIVE", status="FAILED")
    await repo.active_files(7)
    await repo.get_run(1)
    await repo.get_file(1)
    await repo.get_entity(1)
    await repo.get_account(3)
    assert len(db.statements) == 21  # every statement passed the bind check (an empty id list runs no query)
    last = next(s for s, _ in db.statements if "ROW_NUMBER()" in s)
    assert "PARTITION BY r.source_id" in last and "r.status IN ('COMPLETED', 'PARTIAL', 'FAILED')" in last
    stuck = next(s for s, _ in db.statements if "r.updated_at < :before" in s)
    assert "r.status = 'RUNNING'" in stuck
    assert all("result_json" not in s for s, _ in db.statements)  # never listed


# ---- pure helpers ------------------------------------------------------------------------------------


def test_file_stage_details_follow_the_kb_rules():
    now = utc_now()
    details = apply_file_stage_change({}, StageChange("download", "RUNNING"), now=now - timedelta(seconds=2))
    done = apply_file_stage_change(details, StageChange("download", "COMPLETED", metrics={"size_bytes": 9}), now=now)
    assert done["download"]["seconds"] == pytest.approx(2, abs=0.01) and done["download"]["metrics"] == {"size_bytes": 9}
    failed = apply_file_stage_change(done, StageChange("persist", "FAILED", error="boom"), now=now)
    assert failed["persist"]["error"] == "boom" and "started_at" in failed["persist"]
    assert "persist" not in apply_file_stage_change(failed, StageChange("persist", "PENDING"), now=now)
    with pytest.raises(ValueError):
        apply_file_stage_change({}, StageChange("upload", "RUNNING"), now=now)  # a Flow 1 stage
    assert running_file_stage({"download_status": "COMPLETED", "extraction_status": "RUNNING"}) == "extraction"
    assert running_file_stage({"download_status": "COMPLETED", "extraction_status": "COMPLETED", "intelligence_status": "PENDING"}) == "intelligence"


@pytest.mark.parametrize(
    ("record", "types", "expected"),
    [
        ({"email": "  Mona.Adel@Example.COM "}, None, "email:mona.adel@example.com"),
        ({"work_email": "A@B.io"}, None, "email:a@b.io"),
        ({"contact_address": "x@y.z"}, {"contact_address": "email"}, "email:x@y.z"),
        ({"email": "not an email", "phone": "+20 (100) 123-4567"}, None, "phone:201001234567"),
        ({"mobile": "٠١٠٠١٢٣٤٥٦٧"}, None, "phone:01001234567"),  # Arabic-Indic digits
        ({"phone": "123"}, None, None),  # too short to identify anyone
        ({"tax_id": "123-456 789"}, None, "tax_id:123456789"),
        ({"vat_number": "eg 300.1"}, None, "tax_id:EG3001"),
    ],
)
def test_match_key_normalization(record, types, expected):
    assert entity_match_key(record, None, types) == expected


def test_match_key_falls_back_to_the_case_folded_name():
    assert entity_match_key({"name": "x"}, "  ACME   Trading ") == "name:acme trading"
    assert entity_match_key({}, None) is None


def test_display_value_uses_the_schema_display_field_then_name_like_fields():
    display, types = schema_hints({"schemas": {"invoice": {"display_field": "number", "field_types": {"number": "string"}}}})
    assert display["invoice"] == "number" and display["contact"] == "full_name" and types == {"invoice": {"number": "string"}}
    assert entity_display_value({"number": "INV-7", "name": "n"}, "invoice", display) == "INV-7"
    assert entity_display_value({"full_name": "  Mona   Adel "}, "contact", display) == "Mona Adel"
    assert entity_display_value({"title": "CEO"}, "unknown", display) == "CEO"
    assert entity_display_value({"amount": 12.5}, "unknown", display) == "12.5"
    assert entity_display_value({"_meta": {}}, "unknown", display) is None
    assert schema_hints(None)[0]["organization"] == "name"


def test_build_entity_rows_marks_validity_from_the_issues():
    entities = [
        {"full_name": "Mona", "email": "m@x.io", "_meta": {"entity_type": "contact", "confidence": 1.7}},
        {"name": "", "_meta": {"entity_type": "organization", "confidence": 0.2}},
        "not an entity",
    ]
    issues = [
        {"entity_type": "organization", "entity_index": 1, "field": "name", "severity": "error", "code": "missing_required_field", "message": "m"},
        {"entity_type": "contact", "entity_index": 0, "field": None, "severity": "warning", "code": "low_confidence", "message": "w"},
        {"entity_type": "contact", "entity_index": None, "severity": "error", "code": "x", "message": "document-level"},
    ]
    contact, org = build_entity_rows(entities, issues)
    assert contact.is_valid is True and contact.confidence == 1.0 and [i["code"] for i in contact.issues] == ["low_confidence"]
    assert contact.match_key == "email:m@x.io" and contact.fields["_meta"]["entity_type"] == "contact"
    assert org.is_valid is False and org.display_value is None and org.match_key is None


# ---- row -> API ----------------------------------------------------------------------------------------


def test_source_to_out_decrypts_the_ids_and_never_the_secret():
    box = FakeBox()
    row = encrypted_source(box)
    out = source_to_out(row, box=box, counts={"files_active": 2})
    assert (out.tenant_id, out.client_id, out.credentials_readable) == (TENANT_ID, CLIENT_ID, True)
    assert out.client_secret_set and out.file_extensions == [".pdf", ".docx"] and out.recursive is True
    assert out.counts == {"files_active": 2, "files_failed": 0, "files_deleted": 0, "entities_active": 0}
    assert out.updated_at == "2026-09-25T09:30:00.5Z" and out.next_sync_at == "2026-10-01T00:00:00Z"
    assert SECRET not in out.model_dump_json()
    assert set(box.encrypted) == {TENANT_ID, CLIENT_ID}  # the secret was never needed to build the view

    unreadable = source_to_out(row, box=None)
    assert (unreadable.tenant_id, unreadable.client_id, unreadable.credentials_readable) == (None, None, False)
    box.broken = True
    rotated = source_to_out(row, box=box)
    assert rotated.credentials_readable is False and rotated.tenant_id is None
    bare = source_to_out({**row, "tenant_id_enc": None, "client_id_enc": None, "client_secret_set": 0}, box=None)
    assert bare.credentials_readable is True and bare.client_secret_set is False


def test_file_to_out_always_has_the_five_stages_and_only_https_links():
    row = {
        "id": 31, "source_id": 7, "name": "c.pdf", "path": "/CRM", "web_url": "javascript:alert(1)", "size_bytes": 5,
        "modified_at": "2026-09-01T10:00:00", "state": "ACTIVE", "status": "FAILED", "download_status": "COMPLETED",
        "extraction_status": "FAILED", "intelligence_status": "PENDING", "entities_status": "PENDING",
        "persist_status": "PENDING", "failed_stage": "extraction", "error_message": "PDF is password-protected",
        "stage_details": json.dumps({"download": {"started_at": "2026-09-25T10:00:00Z", "finished_at": "2026-09-25T10:00:01Z"}}),
        "warnings_json": json.dumps([{"code": "low_text", "message": "m", "page": 2}, {"x": 1}]), "entity_count": None,
        "is_valid": None, "attempts": 2, "last_run_id": 4, "first_seen_at": "2026-09-20T00:00:00",
        "last_seen_at": None, "processed_at": None, "deleted_at": None, "updated_at": "2026-09-25T10:00:02",
    }
    out = file_to_out(row)
    assert [s.name for s in out.stages] == list(CRM_STAGES)
    assert out.stages[1].error == "PDF is password-protected" and out.stages[0].finished_at == "2026-09-25T10:00:01Z"
    assert out.web_url is None and [w.page for w in out.warnings] == [2] and out.is_valid is None
    assert file_to_out({**row, "web_url": "https://x.sharepoint.com/a.pdf", "is_valid": 1}).web_url.startswith("https://")


def test_entity_to_out_splits_fields_and_provenance_and_flags_deleted_sources():
    row = {
        "id": 1, "source_id": 7, "source_file_id": 31, "account_id": 3, "entity_type": "contact", "display_value": "Mona",
        "match_key": "email:m@x.io", "confidence": 0.9, "is_valid": 1, "status": "ACTIVE",
        "fields_json": json.dumps({"full_name": "Mona", "_meta": {"entity_type": "contact", "fields": {"full_name": {"provenance": [{"page": 1}]}}}}),
        "issues_json": json.dumps([{"code": "low_confidence"}, "junk"]), "created_at": "2026-09-25T10:00:00",
        "updated_at": "2026-09-25T10:00:00", "withdrawn_at": None, "source_name": "Sales", "source_status": "DELETED",
        "file_name": "a.pdf",
    }
    out = entity_to_out(row)
    assert out.fields == {"full_name": "Mona"} and out.provenance["fields"]["full_name"]["provenance"] == [{"page": 1}]
    assert out.issues == [{"code": "low_confidence"}] and out.source_name == "Sales (deleted)" and out.is_valid is True
    assert entity_to_out({**row, "source_status": "ACTIVE"}).source_name == "Sales"


def test_entity_row_defaults():
    row = EntityRow(entity_type="contact", display_value=None, match_key=None, confidence=None, is_valid=None)
    assert row.fields == {} and row.issues == []
