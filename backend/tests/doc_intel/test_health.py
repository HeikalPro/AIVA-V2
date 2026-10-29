"""Monitoring: the six checks, reasons and suggested actions, persistence, transitions, throttling."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import oracledb
import pytest

from backend.doc_intel import health
from backend.doc_intel.constants import HEALTH_COMPONENTS
from backend.doc_intel.health import (
    _safe_url,
    load_activity,
    load_events,
    load_failures,
    load_overview,
    run_checks,
    service_name,
    suggested_action_for,
)
from backend.doc_intel.textutil import utc_now

from .conftest import CORPUS_ID, EMBED_BASE_URL, EMBED_KEY_ENV, PDF_BYTES, Env, FakeExtractionModule, corpus_config

KEY = "sk-test-embedding-key-123456"


def component(overview, key):
    return next(c for c in overview.components if c.key == key)


async def publish_one(env: Env, variant: int = 0) -> int:
    out = await env.upload((f"doc{variant}.pdf", PDF_BYTES + bytes([variant])))
    assert await env.process_next() == "PUBLISHED"
    return out.documents[0].id


@pytest.mark.parametrize(
    ("key", "reason", "expected"),
    [
        ("database", "Application database: ORA-00257: Archiver error. Connect AS SYSDBA only", "disk is full"),
        ("database", "Knowledge-base database: DPY-6005: cannot connect to database", "unreachable"),
        ("database", "Application database: ORA-12541: TNS:no listener", "unreachable"),
        ("database", "Application database: timed out after 5 s", "unreachable"),
        ("database", "Application database: ORA-01017: invalid credential or not authorized", "credentials were rejected"),
        ("embedding", f"Embedding endpoint rejected the API key (HTTP 401) ({EMBED_BASE_URL})", "SOVEREIGNEG_API_KEY"),
        ("embedding", "Embedding endpoint returned HTTP 503 (x)", "provider is failing"),
        ("extraction", "Tesseract Arabic language data (ara) not found", "tesseract-ocr-ara"),
        ("extraction", "document-extractor is not installed in the backend environment", "Install document-extractor"),
        ("knowledge_sync", "2 published documents lost their chunks or queue assignment — Republish", "Republish the affected"),
        ("knowledge_sync", "The import worker is not running", "Restart the backend"),
    ],
)
def test_suggested_actions(key, reason, expected):
    assert expected in (suggested_action_for(key, reason) or "")


def test_no_action_for_unknown_problems():
    assert suggested_action_for("crm", "whatever") is None
    assert suggested_action_for("database", None) is None


@pytest.mark.asyncio
async def test_all_healthy(env: Env):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "text-embedding-3-small"}]})

    env.http_handler = handler
    await publish_one(env)
    overview = await run_checks(env.health_deps())
    assert [c.key for c in overview.components] == [k for k, _ in HEALTH_COMPONENTS]
    assert overview.overall == "HEALTHY" and not overview.stale and not overview.throttled
    for key in ("microsoft_graph", "crm"):
        c = component(overview, key)
        assert (c.status, c.reason) == ("NOT_CONFIGURED", "Part of Phase 2 — not installed yet")
    for key in ("database", "embedding", "extraction", "knowledge_sync"):
        c = component(overview, key)
        assert c.status == "HEALTHY", (key, c.reason)
        assert c.checked_at and c.checked_at.endswith("Z") and c.last_success_at == c.checked_at
    db = component(overview, "database")
    assert db.details["app_pool"]["service"] == "FREEPDB1" and db.details["kb_pool"]["ok"] is True
    emb = component(overview, "embedding")
    (endpoint,) = emb.details["endpoints"]
    assert endpoint["base_url"] == EMBED_BASE_URL and endpoint["key_source"] == f"env {EMBED_KEY_ENV}"
    assert endpoint["status_code"] == 200 and endpoint["models"] == ["text-embedding-3-small"]
    (request,) = seen
    assert request.method == "GET" and str(request.url) == f"{EMBED_BASE_URL}/models"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    sync = component(overview, "knowledge_sync")
    assert sync.details["published_checked"] == 1 and "1 published document verified" in sync.reason
    ext = component(overview, "extraction")
    assert ext.details["ocr_languages_installed"] == ["ara", "eng", "osd"]
    assert "smoke test passed" in ext.reason
    stored = json.dumps([r for r in env.health_repo.rows.values()])
    assert KEY not in stored  # the key never reaches the stored details
    assert len(env.health_repo.events) == 6  # first observation of each component


@pytest.mark.asyncio
async def test_run_is_throttled(env: Env):
    env.settings.health_min_interval_seconds = 30
    first = await run_checks(env.health_deps())
    assert not first.throttled and env.extraction.smoke_calls == 1
    second = await run_checks(env.health_deps())
    assert second.throttled and env.extraction.smoke_calls == 1
    assert [c.status for c in second.components] == [c.status for c in first.components]


@pytest.mark.asyncio
async def test_transitions_create_events_and_count_failures(env: Env):
    deps = env.health_deps()
    await run_checks(deps, component="database")
    env.app_db.error = oracledb.DatabaseError("DPY-6005: cannot connect to database (CONNECTION_ID=x).\n[Errno 111]")
    failed = component(await run_checks(deps, component="database"), "database")
    again = component(await run_checks(deps, component="database"), "database")
    assert failed.status == again.status == "FAILED"
    assert again.consecutive_failures == 2 and again.last_success_at and again.last_failure_at
    assert again.reason == "Application database: DPY-6005: cannot connect to database (CONNECTION_ID=x)."
    assert again.suggested_action == "The database is unreachable: check the Oracle container/host"
    events = await load_events(deps, 10)
    assert [(e.old_status, e.new_status) for e in events] == [("HEALTHY", "FAILED"), (None, "HEALTHY")]
    assert events[0].label == "Database"
    env.app_db.error = None
    recovered = component(await run_checks(deps, component="database"), "database")
    assert recovered.status == "HEALTHY" and recovered.consecutive_failures == 0
    assert len(env.health_repo.events) == 3


@pytest.mark.asyncio
async def test_disk_full_database(env: Env):
    env.kb.ping_error = oracledb.DatabaseError("ORA-00257: Archiver error. Connect AS SYSDBA only until resolved.")
    c = component(await run_checks(env.health_deps(), component="database"), "database")
    assert c.status == "FAILED" and c.reason.startswith("Knowledge-base database: ORA-00257")
    assert c.suggested_action == "The database host disk is full (archiver error): free space on the DB server"


@pytest.mark.asyncio
async def test_embedding_key_rejected(env: Env):
    env.http_handler = lambda request: httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}})
    c = component(await run_checks(env.health_deps(), component="embedding"), "embedding")
    assert c.status == "FAILED" and c.reason.startswith("Embedding endpoint rejected the API key (HTTP 401)")
    assert c.suggested_action == "Update the embedding API key (e.g. SOVEREIGNEG_API_KEY) used by the corpus"
    assert KEY not in json.dumps(c.details)


@pytest.mark.asyncio
async def test_embedding_without_a_key(env: Env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(EMBED_KEY_ENV)
    c = component(await run_checks(env.health_deps(), component="embedding"), "embedding")
    assert c.status == "FAILED" and c.reason.startswith("No API key is configured for the embedding endpoint")
    assert "SOVEREIGNEG_API_KEY" in c.suggested_action
    assert c.details["endpoints"][0]["key_source"] == f"none ({EMBED_KEY_ENV} not set)"


@pytest.mark.asyncio
async def test_embedding_falls_back_to_the_default_key_like_make_embedder(env: Env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(EMBED_KEY_ENV)
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={})

    env.http_handler = handler
    deps = env.health_deps(embedding_settings=type("S", (), {"default_openai_api_key": "sk-default-key"})())
    c = component(await run_checks(deps, component="embedding"), "embedding")
    assert c.status == "HEALTHY" and seen == ["Bearer sk-default-key"]
    assert c.details["endpoints"][0]["key_source"] == "DEFAULT_OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_embedding_endpoint_unreachable(env: Env):
    def handler(request):
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    env.http_handler = handler
    c = component(await run_checks(env.health_deps(), component="embedding"), "embedding")
    assert c.status == "FAILED" and "unreachable" in c.reason
    assert "outbound HTTPS" in c.suggested_action


@pytest.mark.asyncio
async def test_embedding_not_configured_without_any_corpus(env: Env):
    env.repo.accounts.clear()
    c = component(await run_checks(env.health_deps(), component="embedding"), "embedding")
    assert (c.status, c.reason) == ("NOT_CONFIGURED", "No account has a knowledge base yet")


@pytest.mark.asyncio
async def test_embedding_in_database_needs_no_http_call(env: Env):
    cfg = corpus_config()
    cfg["embedder"] = {"type": "oracle", "model": "all_minilm_l12_v2", "dimension": 384}
    env.kb.add_corpus(CORPUS_ID, cfg)
    env.http_handler = lambda request: pytest.fail("no HTTP call expected")
    c = component(await run_checks(env.health_deps(), component="embedding"), "embedding")
    assert c.status == "HEALTHY" and c.details["endpoints"][0]["type"] == "oracle"


@pytest.mark.asyncio
async def test_extraction_not_installed(env: Env):
    deps = env.health_deps(extraction=FakeExtractionModule(available=False, reason="document-extractor is not installed in the backend environment"))
    c = component(await run_checks(deps, component="extraction"), "extraction")
    assert c.status == "FAILED" and c.suggested_action.startswith("Install document-extractor")


@pytest.mark.asyncio
async def test_extraction_without_arabic_ocr_data(env: Env):
    module = FakeExtractionModule()
    module.smoke["info"]["ocr_languages_installed"] = ["eng", "osd"]
    c = component(await run_checks(env.health_deps(extraction=module), component="extraction"), "extraction")
    assert (c.status, c.reason) == ("FAILED", "Tesseract Arabic language data (ara) not found")
    assert "tesseract-ocr-ara" in c.suggested_action


@pytest.mark.asyncio
async def test_extraction_smoke_test_failure(env: Env):
    module = FakeExtractionModule(smoke={"ok": False, "seconds": 90.0, "detail": "Child process timed out after 90 s", "info": {}})
    c = component(await run_checks(env.health_deps(extraction=module), component="extraction"), "extraction")
    assert c.reason == "Extraction smoke test failed: Child process timed out after 90 s"
    assert "timed out" in c.suggested_action


@pytest.mark.asyncio
async def test_knowledge_sync_detects_lost_chunks_and_queue_verticals(env: Env):
    lost_chunks = await publish_one(env, 1)
    lost_queue = await publish_one(env, 2)
    intact = await publish_one(env, 3)
    del env.kb.chunks[(CORPUS_ID, f"kbdoc-{lost_chunks}")]  # e.g. a corpus REINDEX
    cfg = env.kb.configs[CORPUS_ID]
    cfg["queue_groups"]["HALAN"]["verticals"].remove(f"kbdoc-{lost_queue}")  # e.g. seed_queue_groups.py
    c = component(await run_checks(env.health_deps(), component="knowledge_sync"), "knowledge_sync")
    assert c.status == "FAILED"
    assert c.reason.startswith("2 published documents lost their chunks or queue assignment")
    assert c.suggested_action.startswith("Republish the affected documents")
    problems = {p["id"]: p for p in c.details["integrity_problems"]}
    assert set(problems) == {lost_chunks, lost_queue} and intact not in problems
    assert problems[lost_chunks]["chunks"] == 0
    assert problems[lost_queue]["missing_queues"] == ["HALAN"]


@pytest.mark.asyncio
async def test_recent_failures_are_reported_but_do_not_fail_knowledge_sync(env: Env):
    from backend.doc_intel.extraction import ExtractionFailed

    env.extractor.error = ExtractionFailed("corrupt")
    await env.upload(("bad.pdf", PDF_BYTES))
    await env.process_next()
    c = component(await run_checks(env.health_deps(), component="knowledge_sync"), "knowledge_sync")
    assert c.status == "HEALTHY" and "1 import failed in the last 24 h" in c.reason
    assert c.details["failed_last_24h"] == 1 and c.details["recent_failures"][0]["reason"] == "corrupt"


@pytest.mark.asyncio
async def test_knowledge_sync_fails_when_the_worker_is_down_or_documents_are_stuck(env: Env):
    c = component(await run_checks(env.health_deps(worker_running=lambda: False), component="knowledge_sync"), "knowledge_sync")
    assert c.status == "FAILED" and c.reason.startswith("The import worker is not running")
    assert c.suggested_action.startswith("Restart the backend")
    health.reset_throttle()
    old = (utc_now() - timedelta(hours=2)).isoformat()
    env.repo.seed(status="PROCESSING", started_at=old, extraction_status="RUNNING")
    c = component(await run_checks(env.health_deps(), component="knowledge_sync"), "knowledge_sync")
    assert c.status == "FAILED" and "stuck in processing for more than 60 min" in c.reason
    assert c.details["stuck"][0]["started_at"].endswith("Z")


@pytest.mark.asyncio
async def test_a_hanging_check_times_out(env: Env):
    env.settings.health_check_timeout_seconds = 1

    async def hang():
        await asyncio.sleep(10)

    env.repo.status_counts = hang
    c = component(await run_checks(env.health_deps(), component="knowledge_sync"), "knowledge_sync")
    assert (c.status, c.reason) == ("FAILED", "Check timed out after 1 s")


@pytest.mark.asyncio
async def test_fresh_results_are_returned_when_they_cannot_be_stored(env: Env):
    env.health_repo.save_error = oracledb.DatabaseError("ORA-00257: Archiver error")
    env.app_db.error = oracledb.DatabaseError("ORA-00257: Archiver error")
    overview = await run_checks(env.health_deps())
    db = component(overview, "database")
    assert db.status == "FAILED" and db.details["stored"] is False
    assert overview.overall == "FAILED" and "disk is full" in db.suggested_action


@pytest.mark.asyncio
async def test_overview_before_any_check_and_when_stale(env: Env):
    deps = env.health_deps()
    overview = await load_overview(deps)
    assert overview.stale and overview.overall == "HEALTHY" and overview.checked_at is None
    assert {(c.status, c.reason) for c in overview.components} == {("NOT_CONFIGURED", "Not checked yet")}
    await run_checks(deps)
    for row in env.health_repo.rows.values():
        row["checked_at"] = (utc_now() - timedelta(minutes=env.settings.health_stale_minutes + 1)).isoformat()
    assert (await load_overview(deps)).stale


@pytest.mark.asyncio
async def test_overview_when_stored_results_are_unreadable(env: Env):
    env.health_repo.load_error = oracledb.DatabaseError("DPY-6005: cannot connect to database")
    overview = await load_overview(env.health_deps())
    db = component(overview, "database")
    assert overview.overall == "FAILED" and overview.stale
    assert db.reason == "Could not read stored health results: DPY-6005: cannot connect to database"
    assert db.suggested_action == "The database is unreachable: check the Oracle container/host"


@pytest.mark.asyncio
async def test_single_component_run_and_unknown_component(env: Env):
    overview = await run_checks(env.health_deps(), component="crm")
    assert component(overview, "crm").reason == "Part of Phase 2 — not installed yet"
    assert component(overview, "database").reason == "Not checked yet"
    with pytest.raises(ValueError):
        await run_checks(env.health_deps(), component="nope")


@pytest.mark.asyncio
async def test_failures_and_activity_feeds(env: Env):
    from backend.doc_intel.extraction import ExtractionFailed

    published = await publish_one(env, 1)
    env.extractor.error = ExtractionFailed("PDF is password-protected")
    out = await env.upload(("locked.pdf", PDF_BYTES + b"2"))
    await env.process_next()
    failed = out.documents[0].id
    deps = env.health_deps()
    await run_checks(deps, component="database")

    failures = await load_failures(deps, 7)
    assert failures.days == 7
    (item,) = failures.items
    assert (item.kind, item.id, item.title, item.stage) == ("kb_document", failed, "locked.pdf", "extraction")
    assert item.reason == "PDF is password-protected" and item.account_name == "Hallan"

    activity = await load_activity(deps, 10)
    kinds = {(i.kind, i.ref_id): i for i in activity.items}
    assert kinds[("kb_document", failed)].level == "error"
    assert "failed at extraction: PDF is password-protected" in kinds[("kb_document", failed)].message
    assert kinds[("kb_document", published)].message.startswith('Published "doc1.pdf" to HALAN')
    assert any(i.kind == "health" and i.message.startswith("Database: — → HEALTHY") for i in activity.items)
    stamps = [i.occurred_at for i in activity.items]
    assert stamps == sorted(stamps, reverse=True)


def test_service_name_never_exposes_the_connect_string():
    assert service_name("db.internal:1521/FREEPDB1") == "FREEPDB1"
    assert service_name("(DESCRIPTION=(ADDRESS=(HOST=h)(PORT=1))(CONNECT_DATA=(SERVICE_NAME=orclpdb)))") == "orclpdb"
    assert service_name("mydb_high") == "mydb_high"
    assert service_name("host/svc:pooled") == "svc"
    assert service_name(None) is None


def test_safe_url_strips_credentials_and_query():
    assert _safe_url("https://user:pass@api.example.com:8443/v1?key=secret#x") == "https://api.example.com:8443/v1"


@pytest.mark.asyncio
async def test_extraction_busy_smoke_is_healthy_and_says_so(env: Env):
    module = FakeExtractionModule()
    module.smoke.update({"ok": True, "busy": True, "seconds": 0.0, "detail": "A document is being extracted right now"})
    c = component(await run_checks(env.health_deps(extraction=module), component="extraction"), "extraction")
    assert c.status == "HEALTHY"
    assert "smoke test skipped" in (c.reason or "")
    assert c.details["smoke_test"]["busy"] is True
