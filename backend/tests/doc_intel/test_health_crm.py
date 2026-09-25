"""Monitoring, Phase 2: the Microsoft and CRM components, the SharePoint side of "Knowledge sync",
and the crm_file / sync_run kinds of the failures and activity feeds."""
from __future__ import annotations

import json
from datetime import timedelta

import oracledb
import pytest

from backend.doc_intel import health
from backend.doc_intel.crm_extraction import CrmExtractionFailed
from backend.doc_intel.extraction import ExtractionFailed
from backend.doc_intel.graph_source import GraphFailed
from backend.doc_intel.health import (
    CRM_STORE_ACTION,
    SYNC_FAILED_ACTION,
    SYNC_OVERDUE_ACTION,
    load_activity,
    load_failures,
    run_checks,
)
from backend.doc_intel.kb_repo import parse_utc
from backend.doc_intel.textutil import iso_utc, utc_now

from ._crm_fakes import (  # noqa: F401  (crm_env is a fixture)
    CLIENT_ID,
    KEY_MISSING_REASON,
    SECRET,
    TARGET,
    TENANT_ID,
    CrmEnv,
    crm_env,
    old,
    remote,
)
from .conftest import PDF_BYTES

SALES = "https://contoso.sharepoint.com/sites/Sales"


def component(overview, key):
    return next(c for c in overview.components if c.key == key)


async def check(crm: CrmEnv, key: str, **deps):
    health.reset_throttle()
    return component(await run_checks(crm.health_deps(**deps), component=key), key)


# ---- Microsoft connection --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase2_components_are_not_configured_without_sources(crm_env: CrmEnv):
    crm_env.seed_source(status="DISABLED")
    overview = await run_checks(crm_env.health_deps())
    assert (component(overview, "microsoft_graph").status, component(overview, "microsoft_graph").reason) == (
        "NOT_CONFIGURED", "No SharePoint source is configured",
    )
    crm = component(overview, "crm")
    assert crm.status == "HEALTHY"  # a disabled source is still a source whose entities are stored
    crm_env.repo.sources.clear()
    crm = await check(crm_env, "crm")
    assert (crm.status, crm.reason) == ("NOT_CONFIGURED", "No SharePoint source is configured")
    assert component(overview, "knowledge_sync").status == "HEALTHY"


@pytest.mark.asyncio
async def test_microsoft_is_healthy_when_every_active_source_signs_in_and_resolves(crm_env: CrmEnv):
    crm_env.seed_source(name="Sales")
    crm_env.seed_source(name="Legal", site_url="https://contoso.sharepoint.com/sites/Legal")
    crm_env.seed_source(name="Off", status="DISABLED")
    c = await check(crm_env, "microsoft_graph")
    assert (c.status, c.reason) == ("HEALTHY", "2 SharePoint sources reachable (sign-in and folder)")
    assert [(s["name"], s["ok"], s["reason"]) for s in c.details["sources"]] == [("Sales", True, None), ("Legal", True, None)]
    assert [call[0] for call in crm_env.graph.calls] == ["token", "resolve", "token", "resolve"]  # never a listing
    assert crm_env.graph.closed == 2
    assert crm_env.repo.sources[1]["resolved_site_id"] is None  # a health check caches nothing
    stored = json.dumps(crm_env.env.health_repo.rows)
    assert SECRET not in stored and TENANT_ID not in stored and CLIENT_ID not in stored


@pytest.mark.asyncio
async def test_microsoft_reports_the_first_failure_with_its_suggested_action(crm_env: CrmEnv, monkeypatch):
    crm_env.seed_source(name="Sales")
    crm_env.seed_source(name="Legal", site_url="https://contoso.sharepoint.com/sites/Legal")
    crm_env.seed_source(name="HR", site_url="https://contoso.sharepoint.com/sites/HR")
    action = "Grant Sites.Read.All + Files.Read.All (Application) and admin consent"

    def resolve(site_url, drive_name, folder_path):
        if not site_url.endswith("/Sales"):
            raise GraphFailed("Microsoft Graph denied access to the SharePoint site (HTTP 403)", code="forbidden",
                              suggested_action=action, status_code=403)
        return TARGET

    monkeypatch.setattr(crm_env.graph, "resolve", resolve)
    c = await check(crm_env, "microsoft_graph")
    assert c.status == "FAILED"
    assert c.reason == "Legal: Microsoft Graph denied access to the SharePoint site (HTTP 403) (and 1 more)"
    assert c.suggested_action == action and c.consecutive_failures == 1
    assert {s["name"]: s["ok"] for s in c.details["sources"]} == {"Sales": True, "Legal": False, "HR": False}
    assert c.details["sources"][1]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_a_sign_in_error_is_reported_and_scrubbed(crm_env: CrmEnv):
    crm_env.seed_source(name="Sales")
    crm_env.graph.token_error = GraphFailed(
        f"Microsoft rejected the client secret (AADSTS7000215) {SECRET}", code="auth_invalid_secret",
        suggested_action="Create a new client secret in Entra ID and paste it in Integrations",
    )
    c = await check(crm_env, "microsoft_graph")
    assert c.status == "FAILED" and c.reason == "Sales: Microsoft rejected the client secret (AADSTS7000215) [redacted]"
    assert c.suggested_action.startswith("Create a new client secret")
    assert SECRET not in json.dumps(crm_env.env.health_repo.rows) + json.dumps(crm_env.env.health_repo.events)


@pytest.mark.asyncio
async def test_missing_key_and_undecryptable_credentials(crm_env: CrmEnv):
    crm_env.seed_source(name="Sales")
    crm_env.key_missing = True
    c = await check(crm_env, "microsoft_graph")
    assert (c.status, c.reason) == ("FAILED", KEY_MISSING_REASON)
    assert "DOC_INTEL_SECRETS_KEY" in c.suggested_action and c.details["sources"][0]["ok"] is False
    crm_env.key_missing = False
    crm_env.box.broken = True
    c = await check(crm_env, "microsoft_graph")
    assert c.status == "FAILED" and c.reason == "Sales: A stored credential could not be decrypted"
    assert "enter the Tenant ID" in c.suggested_action
    assert crm_env.graph.calls == []


# ---- CRM connection ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crm_is_healthy_and_counts_the_store(crm_env: CrmEnv):
    crm_env.graph.files = [remote("a")]
    source_id = crm_env.seed_source()
    await crm_env.sync(source_id)
    c = await check(crm_env, "crm")
    assert (c.status, c.reason) == ("HEALTHY", "CRM store reachable · 2 active entities from 1 tracked file")
    assert c.details == {"sources": 1, "entities_active": 2, "files_active": 1}


@pytest.mark.asyncio
async def test_crm_fails_while_the_latest_sync_could_not_persist(crm_env: CrmEnv, monkeypatch):
    crm_env.graph.files = [remote("a")]
    source_id = crm_env.seed_source()
    original = crm_env.repo.complete_file
    failing = {"on": True}

    async def complete_file(file_id, **kw):
        if failing["on"]:
            raise oracledb.DatabaseError("ORA-01653: unable to extend table AI_ASSISTANT.AIVA_CRM_ENTITIES")
        return await original(file_id, **kw)

    monkeypatch.setattr(crm_env.repo, "complete_file", complete_file)
    await crm_env.sync(source_id)
    c = await check(crm_env, "crm")
    assert c.status == "FAILED"
    assert c.reason == (
        "1 file could not be stored in the CRM store in the latest sync: Could not store the entities in the CRM store: "
        "ORA-01653: unable to extend table AI_ASSISTANT.AIVA_CRM_ENTITIES"
    )
    assert c.suggested_action.startswith("Check the Database component") and c.details["persist_failures"][0]["files"] == 1
    failing["on"] = False
    await crm_env.sync(source_id)  # the failed file is retried and stored
    assert (await check(crm_env, "crm")).status == "HEALTHY"


@pytest.mark.asyncio
async def test_a_deleted_sources_history_fails_neither_crm_nor_the_failures_feed(crm_env: CrmEnv, monkeypatch):
    crm_env.graph.files = [remote("a")]
    gone = crm_env.seed_source(name="Gone")

    async def complete_file(file_id, **kw):
        raise oracledb.DatabaseError("ORA-01653: unable to extend table")

    monkeypatch.setattr(crm_env.repo, "complete_file", complete_file)
    await crm_env.sync(gone)
    assert (await check(crm_env, "crm")).status == "FAILED"
    assert [f.kind for f in (await load_failures(crm_env.health_deps(), 7)).items] == ["crm_file"]
    crm_env.repo.sources[gone]["status"] = "DELETED"
    crm_env.seed_source(name="Live")
    assert (await check(crm_env, "crm")).status == "HEALTHY"
    assert (await load_failures(crm_env.health_deps(), 7)).items == []


@pytest.mark.asyncio
async def test_crm_fails_when_the_store_is_unreachable(crm_env: CrmEnv):
    crm_env.seed_source()
    crm_env.repo.raise_on["store_stats"] = oracledb.DatabaseError("DPY-6005: cannot connect to database")
    c = await check(crm_env, "crm")
    assert (c.status, c.reason, c.suggested_action) == (
        "FAILED", "CRM store unreachable: DPY-6005: cannot connect to database", CRM_STORE_ACTION,
    )


# ---- Knowledge sync: the SharePoint side --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_last_sync_fails_knowledge_sync_until_a_sync_succeeds(crm_env: CrmEnv):
    source_id = crm_env.seed_source(name="Sales")
    crm_env.graph.token_error = GraphFailed("The client secret has expired (AADSTS7000222)", code="auth_expired_secret")
    await crm_env.sync(source_id)
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "FAILED" and c.reason.startswith("Last sync of 'Sales' failed: The client secret has expired (AADSTS7000222)")
    assert c.suggested_action == SYNC_FAILED_ACTION
    (entry,) = c.details["sharepoint"]["sources"]
    assert (entry["name"], entry["last_run_status"]) == ("Sales", "FAILED") and entry["last_run_at"].endswith("Z")
    crm_env.graph.token_error = None
    await crm_env.sync(source_id)
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "HEALTHY" and "1 SharePoint source" in c.reason


@pytest.mark.asyncio
async def test_a_run_without_heartbeat_is_stuck(crm_env: CrmEnv):
    source_id = crm_env.seed_source(name="Sales")
    run_id = await crm_env.repo.create_run(source_id, trigger_type="SCHEDULED", triggered_by=None)
    minutes = crm_env.settings.stuck_after_minutes
    crm_env.repo.runs[run_id].update(status="RUNNING", started_at=old(minutes + 30), updated_at=old(minutes + 5))
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "FAILED" and f"1 sync run stuck: no heartbeat for more than {minutes} min" in c.reason
    assert c.details["sharepoint"]["stuck_runs"][0]["id"] == run_id
    crm_env.repo.runs[run_id]["updated_at"] = old(1)  # heartbeating again: a long sync is not stuck
    assert (await check(crm_env, "knowledge_sync")).status == "HEALTHY"


@pytest.mark.asyncio
async def test_an_overdue_schedule_fails_only_while_the_scheduler_is_on(crm_env: CrmEnv):
    due = (utc_now() - timedelta(days=2)).replace(microsecond=0)
    crm_env.seed_source(name="Sales", sync_enabled=1, next_sync_at=due)
    crm_env.settings.scheduler_enabled = True
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "FAILED" and c.reason.startswith(f"Scheduled sync is overdue for 'Sales' (was due {iso_utc(due)})")
    assert c.suggested_action == SYNC_OVERDUE_ACTION
    crm_env.settings.scheduler_enabled = False
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "HEALTHY" and "Automatic syncs are off (DOC_INTEL_SCHEDULER_ENABLED=false)" in c.reason
    assert c.details["sharepoint"]["scheduler_enabled"] is False
    crm_env.settings.scheduler_enabled = True
    crm_env.repo.sources[1]["next_sync_at"] = (utc_now() - timedelta(hours=3)).isoformat()  # late by hours only
    assert (await check(crm_env, "knowledge_sync")).status == "HEALTHY"


@pytest.mark.asyncio
async def test_the_sync_worker_must_run_and_import_problems_come_first(crm_env: CrmEnv):
    source_id = crm_env.seed_source(name="Sales")
    c = await check(crm_env, "knowledge_sync", sync_worker_running=lambda: False)
    assert c.status == "FAILED" and c.reason.startswith("The sync worker is not running")
    assert c.suggested_action.startswith("Restart the backend")
    crm_env.graph.token_error = GraphFailed("Could not reach Microsoft Graph (ConnectError)", code="network")
    await crm_env.sync(source_id)
    c = await check(crm_env, "knowledge_sync", worker_running=lambda: False)
    assert c.reason.startswith("The import worker is not running; Last sync of 'Sales' failed")
    assert c.suggested_action.startswith("Restart the backend")  # the first problem's action


@pytest.mark.asyncio
async def test_a_broken_sharepoint_state_query_is_a_problem_not_a_crash(crm_env: CrmEnv):
    crm_env.seed_source()
    crm_env.repo.raise_on["list_sources"] = oracledb.DatabaseError("DPY-6005: cannot connect to database")
    c = await check(crm_env, "knowledge_sync")
    assert c.status == "FAILED" and c.reason == "SharePoint sync state unavailable: DPY-6005: cannot connect to database"
    assert c.details["published_checked"] == 0  # the knowledge-document side was still checked


# ---- feeds -----------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failures_and_activity_include_sharepoint_files_and_runs(crm_env: CrmEnv):
    env = crm_env.env
    env.extractor.error = ExtractionFailed("PDF is password-protected")
    await env.upload(("locked.pdf", PDF_BYTES + b"1"))
    await env.process_next()

    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("File is corrupt", stage="extraction")
    sales = crm_env.seed_source(name="Sales")
    await crm_env.sync(sales)
    legal = crm_env.seed_source(name="Legal", site_url="https://contoso.sharepoint.com/sites/Legal")
    crm_env.graph.token_error = GraphFailed("Microsoft rejected the client secret (AADSTS7000215)", code="auth_invalid_secret")
    run_id, _ = await crm_env.sync(legal)
    deps = crm_env.health_deps()

    failures = (await load_failures(deps, 7)).items
    assert {f.kind for f in failures} == {"kb_document", "crm_file", "sync_run"}
    crm_file = next(f for f in failures if f.kind == "crm_file")
    assert (crm_file.title, crm_file.stage, crm_file.reason, crm_file.account_name) == ("b.pdf (Sales)", "extraction", "File is corrupt", "Hallan")
    run = next(f for f in failures if f.kind == "sync_run")
    assert (run.id, run.title, run.reason) == (run_id, "Sync of Legal (manual)", "Microsoft rejected the client secret (AADSTS7000215)")
    stamps = [parse_utc(f.occurred_at) for f in failures]
    assert stamps == sorted(stamps, reverse=True)

    activity = (await load_activity(deps, 50)).items
    kinds = {i.kind for i in activity}
    assert {"kb_document", "crm_file", "sync_run"} <= kinds
    messages = {(i.kind, i.ref_id): (i.level, i.message) for i in activity}
    assert messages[("sync_run", run_id)] == ("error", "Sync of 'Legal' failed: Microsoft rejected the client secret (AADSTS7000215)")
    b_id = crm_env.file_by_item("b")["id"]
    assert messages[("crm_file", b_id)] == ("error", 'Processing of "b.pdf" (Sales) failed at extraction: File is corrupt')
    a_id = crm_env.file_by_item("a")["id"]
    assert messages[("crm_file", a_id)] == ("info", 'Processed "a.pdf" (Sales) (2 entities)')
    partial = next(i for i in activity if i.kind == "sync_run" and i.ref_id != run_id)
    assert partial.level == "warning" and partial.message.startswith("Sync of 'Sales' finished with problems (2 new, 0 changed, 0 deleted, 1 failed)")


@pytest.mark.asyncio
async def test_feeds_degrade_when_the_sharepoint_queries_fail(crm_env: CrmEnv):
    env = crm_env.env
    env.extractor.error = ExtractionFailed("PDF is password-protected")
    await env.upload(("locked.pdf", PDF_BYTES + b"2"))
    await env.process_next()

    async def broken(*args, **kwargs):
        raise oracledb.DatabaseError("DPY-6005: cannot connect to database")

    crm_env.repo.failed_files_since = broken
    crm_env.repo.run_activity = broken
    deps = crm_env.health_deps()
    assert [f.kind for f in (await load_failures(deps, 7)).items] == ["kb_document"]
    assert {i.kind for i in (await load_activity(deps, 20)).items} == {"kb_document"}
