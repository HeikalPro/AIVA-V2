"""HTTP API: /api/doc-intel/status and /monitoring/* (Super Admin + Developer), plus route guard coverage."""
from __future__ import annotations

import pytest
from fastapi.dependencies.utils import get_dependant

from backend.auth.role_constants import ROLE_DEVELOPER, ROLE_SUPER_ADMIN
from backend.doc_intel import guards
from backend.doc_intel.runtime import NOT_INSTALLED_DETAIL, NOT_STARTED_DETAIL

from .conftest import NON_PLATFORM_ROLES, Api, FakeExtractionModule, api_client, doc_intel_routes, make_runtime
# Phase 2 routes: their full role matrix (401 / 403 per role / allowed) is in test_api_integrations.py.
from .test_api_integrations import ADMIN_ROUTES as INTEGRATION_ADMIN_ROUTES
from .test_api_integrations import INTEGRATION_ROUTES
from .test_api_integrations import MONITOR_ROUTES as INTEGRATION_MONITOR_ROUTES

BASE = "/api/doc-intel"
MONITORING_ROUTES = {
    ("GET", f"{BASE}/status"),
    ("GET", f"{BASE}/monitoring/health"),
    ("POST", f"{BASE}/monitoring/health/run"),
    ("GET", f"{BASE}/monitoring/events"),
    ("GET", f"{BASE}/monitoring/failures"),
    ("GET", f"{BASE}/monitoring/activity"),
}


def _dependency_calls(dependant) -> set:
    calls = set()
    for dep in dependant.dependencies:
        calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


@pytest.mark.asyncio
async def test_every_doc_intel_route_is_role_guarded(api: Api):
    routes = doc_intel_routes(api.app)
    assert routes, "no doc-intel routes found"
    for method, path, endpoint in routes:
        calls = _dependency_calls(get_dependant(path=path, call=endpoint))
        if path.startswith(f"{BASE}/kb-documents") or (method, path) in INTEGRATION_ADMIN_ROUTES:
            assert guards.admin_only in calls, (method, path)
            assert guards.admin_or_developer not in calls, (method, path)
        elif (method, path) in INTEGRATION_MONITOR_ROUTES:
            assert guards.admin_or_developer in calls and guards.admin_only not in calls, (method, path)
        else:
            assert (method, path) in MONITORING_ROUTES, f"unclassified route {method} {path}: add it to a role matrix"
            assert guards.admin_or_developer in calls, (method, path)


def test_the_guard_check_catches_an_unguarded_route():
    from fastapi import APIRouter, FastAPI

    router = APIRouter(prefix="/api/doc-intel")

    @router.get("/kb-documents/leak")
    async def leak() -> dict:
        return {}

    @router.get("/kb-documents/ok")
    async def ok(user: guards.AdminUser) -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    found = {path: guards.admin_only in _dependency_calls(get_dependant(path=path, call=endpoint))
             for _, path, endpoint in doc_intel_routes(app)}
    assert found == {"/api/doc-intel/kb-documents/leak": False, "/api/doc-intel/kb-documents/ok": True}


@pytest.mark.asyncio
async def test_role_matrix_for_every_monitoring_route(api: Api):
    routes = [
        (m, p) for m, p, _ in doc_intel_routes(api.app)
        if not p.startswith(f"{BASE}/kb-documents") and (m, p) not in INTEGRATION_ROUTES
    ]
    assert set(routes) == MONITORING_ROUTES
    for method, path in sorted(routes):
        assert (await api.call(method, path, role=None)).status_code == 401, (method, path)
        for role in NON_PLATFORM_ROLES:
            r = await api.call(method, path, role=role)
            assert r.status_code == 403, (method, path, role)
            assert r.json()["detail"] == "Requires one of roles: DEVELOPER"
        for role in (ROLE_DEVELOPER, ROLE_SUPER_ADMIN):
            r = await api.call(method, path, role=role)
            assert r.status_code == 200, (method, path, role, r.text)


@pytest.mark.asyncio
async def test_status_when_installed(api: Api):
    body = (await api.call("GET", f"{BASE}/status", role=ROLE_DEVELOPER)).json()
    assert body == {
        "enabled": True,
        "installed": True,
        "schema_version": "001",
        "worker_running": True,
        "extraction_available": True,
        "extraction_unavailable_reason": None,
        "detail": None,
        "max_upload_mb": 50,
        "max_files_per_upload": 20,
        "allowed_extensions": [".pdf", ".docx"],
        # Phase 2: this runtime has no SharePoint sync (V002) and the test settings no key.
        "crm_installed": False,
        "sync_worker_running": False,
        "scheduler_enabled": False,
        "secrets_key_configured": False,
    }


@pytest.mark.asyncio
async def test_status_when_not_installed_or_not_started(env):
    runtime = make_runtime(
        env,
        installed=False,
        schema_version=None,
        service=None,
        health=None,
        worker=None,
        detail="Missing tables: AIVA_KB_DOCUMENTS",
        extraction=FakeExtractionModule(available=False, reason="document-extractor is not installed"),
    )
    async with api_client(env, runtime) as api:
        body = (await api.call("GET", f"{BASE}/status")).json()
        assert body["installed"] is False and body["worker_running"] is False
        assert body["detail"] == f"{NOT_INSTALLED_DETAIL} (Missing tables: AIVA_KB_DOCUMENTS)"
        assert body["extraction_available"] is False
        assert body["extraction_unavailable_reason"] == "document-extractor is not installed"
        r = await api.call("GET", f"{BASE}/monitoring/health", role=ROLE_DEVELOPER)
        assert (r.status_code, r.json()["detail"]) == (503, NOT_INSTALLED_DETAIL)
    async with api_client(env, None) as api:
        body = (await api.call("GET", f"{BASE}/status")).json()
        assert body["installed"] is False and body["detail"] == NOT_STARTED_DETAIL


@pytest.mark.asyncio
async def test_health_read_run_and_throttle(api: Api):
    before = (await api.call("GET", f"{BASE}/monitoring/health", role=ROLE_DEVELOPER)).json()
    assert before["stale"] is True and before["checked_at"] is None
    assert {c["reason"] for c in before["components"]} == {"Not checked yet"}

    api.env.settings.health_min_interval_seconds = 30
    run = await api.call("POST", f"{BASE}/monitoring/health/run", role=ROLE_DEVELOPER)
    body = run.json()
    assert run.status_code == 200 and body["throttled"] is False and body["overall"] == "HEALTHY"
    assert [c["key"] for c in body["components"]] == [
        "microsoft_graph", "crm", "knowledge_sync", "extraction", "embedding", "database",
    ]
    component = body["components"][-1]
    assert set(component) == {
        "key", "label", "status", "reason", "suggested_action", "checked_at", "last_success_at",
        "last_failure_at", "consecutive_failures", "latency_ms", "details",
    }
    again = (await api.call("POST", f"{BASE}/monitoring/health/run")).json()
    assert again["throttled"] is True
    stored = (await api.call("GET", f"{BASE}/monitoring/health")).json()
    assert stored["stale"] is False and stored["checked_at"] == body["checked_at"]


@pytest.mark.asyncio
async def test_run_one_component_and_reject_unknown_ones(api: Api):
    body = (await api.call("POST", f"{BASE}/monitoring/health/run", params={"component": "database"})).json()
    statuses = {c["key"]: c["reason"] for c in body["components"]}
    assert statuses["extraction"] == "Not checked yet" and statuses["database"].startswith("Application and knowledge-base")
    r = await api.call("POST", f"{BASE}/monitoring/health/run", params={"component": "nope"})
    assert (r.status_code, r.json()["detail"]) == (400, "Unknown component: nope")


@pytest.mark.asyncio
async def test_events_failures_and_activity(api: Api):
    from backend.doc_intel.extraction import ExtractionFailed

    await api.call("POST", f"{BASE}/monitoring/health/run", params={"component": "database"})
    api.env.extractor.error = ExtractionFailed("PDF is password-protected")
    await api.env.upload(("locked.pdf", b"%PDF-1.7 locked"))
    await api.env.process_next()

    events = (await api.call("GET", f"{BASE}/monitoring/events", params={"limit": 5}, role=ROLE_DEVELOPER)).json()
    assert events[0]["component_key"] == "database" and events[0]["label"] == "Database"
    assert events[0]["old_status"] is None and events[0]["new_status"] == "HEALTHY"

    failures = (await api.call("GET", f"{BASE}/monitoring/failures", params={"days": 7}, role=ROLE_DEVELOPER)).json()
    assert failures["days"] == 7 and failures["items"][0]["reason"] == "PDF is password-protected"
    assert (await api.call("GET", f"{BASE}/monitoring/failures", params={"days": 0})).status_code == 422
    assert (await api.call("GET", f"{BASE}/monitoring/failures", params={"days": 91})).status_code == 422

    activity = (await api.call("GET", f"{BASE}/monitoring/activity", params={"limit": 10}, role=ROLE_DEVELOPER)).json()
    assert {i["kind"] for i in activity["items"]} == {"kb_document", "health"}
    assert (await api.call("GET", f"{BASE}/monitoring/activity", params={"limit": 501})).status_code == 422
