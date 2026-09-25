"""HTTP API: /api/doc-intel/sources, /source-files and /crm (Phase 2, SharePoint -> CRM).

Through a test app that mounts only the doc-intel router, on the in-memory fakes of _crm_fakes.
The route sets below are the role matrix of every Phase 2 route; the Phase 1 route-guard tests
import them, so a new route fails until it is classified here deliberately.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from backend.auth.role_constants import ROLE_DEVELOPER, ROLE_SUPER_ADMIN
from backend.doc_intel.crm_extraction import CrmExtractionFailed
from backend.doc_intel.runtime import CRM_NOT_INSTALLED_DETAIL, NOT_INSTALLED_DETAIL

from ._crm_fakes import (  # noqa: F401  (crm_env is a fixture)
    CLIENT_ID,
    KEY_MISSING_REASON,
    NEW_SECRET,
    SECRET,
    TENANT_ID,
    CrmEnv,
    crm_env,
    remote,
    source_body,
)
from .conftest import NON_PLATFORM_ROLES, Api, api_client, doc_intel_routes

BASE = "/api/doc-intel"
SA, DEV = ROLE_SUPER_ADMIN, ROLE_DEVELOPER

# Super Admin only: source administration, "Sync now", retry, and the CRM entities (business data).
ADMIN_ROUTES = frozenset(
    {
        ("POST", f"{BASE}/sources"),
        ("PATCH", f"{BASE}/sources/{{source_id}}"),
        ("DELETE", f"{BASE}/sources/{{source_id}}"),
        ("POST", f"{BASE}/sources/{{source_id}}/sync"),
        ("POST", f"{BASE}/source-files/{{file_id}}/retry"),
        ("GET", f"{BASE}/crm/entities"),
        ("GET", f"{BASE}/crm/entities/{{entity_id}}"),
    }
)
# Super Admin + Developer: read-only views and the connection diagnostics.
MONITOR_ROUTES = frozenset(
    {
        ("GET", f"{BASE}/sources"),
        ("GET", f"{BASE}/sources/{{source_id}}"),
        ("POST", f"{BASE}/sources/{{source_id}}/test"),
        ("GET", f"{BASE}/sources/{{source_id}}/runs"),
        ("GET", f"{BASE}/sources/{{source_id}}/files"),
    }
)
INTEGRATION_ROUTES = ADMIN_ROUTES | MONITOR_ROUTES
_PREFIXES = (f"{BASE}/sources", f"{BASE}/source-files", f"{BASE}/crm")

SA_STATUS = {
    ("POST", f"{BASE}/sources"): 201,
    ("DELETE", f"{BASE}/sources/{{source_id}}"): 204,
    ("POST", f"{BASE}/sources/{{source_id}}/sync"): 202,
    ("POST", f"{BASE}/source-files/{{file_id}}/retry"): 202,
}


@pytest_asyncio.fixture
async def crm_api(crm_env: CrmEnv) -> AsyncIterator[Api]:
    async with api_client(crm_env.env, crm_env.runtime()) as harness:
        yield harness


async def seeded(crm_api: Api, crm_env: CrmEnv) -> dict[str, int]:
    """A source synced once (file a COMPLETED with entities, file b FAILED), plus a spare source."""
    created = await crm_api.call("POST", f"{BASE}/sources", json=source_body())
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]
    crm_env.graph.files = [remote("a"), remote("b")]
    crm_env.extractor.errors["b.pdf"] = CrmExtractionFailed("File is corrupt", stage="extraction")
    await crm_env.sync(source_id)
    spare = (await crm_api.call("POST", f"{BASE}/sources", json=source_body(name="Spare"))).json()["id"]
    return {
        "source_id": source_id,
        "spare_id": spare,
        "file_id": crm_env.file_by_item("b")["id"],
        "entity_id": crm_env.active_entities("a")[0]["id"],
    }


def url_for(method: str, path: str, ids: dict[str, int]) -> str:
    source = ids["spare_id"] if method == "DELETE" else ids["source_id"]
    return (
        path.replace("{source_id}", str(source))
        .replace("{file_id}", str(ids["file_id"]))
        .replace("{entity_id}", str(ids["entity_id"]))
    )


def body_for(method: str, path: str) -> dict:
    if method == "POST" and path == f"{BASE}/sources":
        return {"json": source_body(name="Matrix")}
    if method == "PATCH":
        return {"json": {"name": "Renamed"}}
    return {}


# ---- role matrix -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_role_matrix_for_every_integration_route(crm_api: Api, crm_env: CrmEnv):
    ids = await seeded(crm_api, crm_env)
    routes = {(m, p) for m, p, _ in doc_intel_routes(crm_api.app) if p.startswith(_PREFIXES)}
    assert routes == INTEGRATION_ROUTES  # a new route must be added to this matrix deliberately
    for method, path in sorted(routes):
        url, kwargs = url_for(method, path, ids), body_for(method, path)
        assert (await crm_api.call(method, url, role=None, **kwargs)).status_code == 401, (method, path)
        for role in NON_PLATFORM_ROLES:
            r = await crm_api.call(method, url, role=role, **kwargs)
            expected = "Requires one of roles: SUPER_ADMIN" if (method, path) in ADMIN_ROUTES else "Requires one of roles: DEVELOPER"
            assert (r.status_code, r.json()["detail"]) == (403, expected), (method, path, role)
        developer = await crm_api.call(method, url, role=DEV, **kwargs)
        if (method, path) in ADMIN_ROUTES:
            assert developer.status_code == 403 and developer.json()["detail"] == "Requires one of roles: SUPER_ADMIN", (method, path)
        else:
            assert developer.status_code == 200, (method, path, developer.text)
    # Nothing above changed anything (every refusal came before the handler) ...
    assert crm_env.source(ids["source_id"])["name"] == "Sales contracts" and len(crm_env.repo.sources) == 2
    # ... and the Super Admin reaches every route ("Sync now" before the retry, which would reuse
    # the queued run, and the delete, of the spare source, last).
    last = [("POST", f"{BASE}/source-files/{{file_id}}/retry"), ("DELETE", f"{BASE}/sources/{{source_id}}")]
    for method, path in [r for r in sorted(routes) if r not in last] + last:
        r = await crm_api.call(method, url_for(method, path, ids), role=SA, **body_for(method, path))
        assert r.status_code == SA_STATUS.get((method, path), 200), (method, path, r.status_code, r.text)
    assert crm_env.source(ids["spare_id"])["status"] == "DELETED" and crm_env.source(ids["source_id"])["name"] == "Renamed"


@pytest.mark.asyncio
async def test_routes_answer_503_until_v002_is_installed(crm_env: CrmEnv):
    for runtime, detail in (
        (crm_env.runtime(crm_installed=False, sync_service=None, sync_worker=None), CRM_NOT_INSTALLED_DETAIL),
        (crm_env.runtime(sync_service=None, crm_detail="SharePoint sync failed to start: ImportError: msal"),
         "SharePoint sync failed to start: ImportError: msal"),
        (crm_env.runtime(installed=False, service=None, health=None, crm_installed=False), NOT_INSTALLED_DETAIL),
    ):
        async with api_client(crm_env.env, runtime) as api:
            for method, path in sorted(INTEGRATION_ROUTES):
                url = path.replace("{source_id}", "1").replace("{file_id}", "1").replace("{entity_id}", "1")
                r = await api.call(method, url, role=SA, **body_for(method, path))
                assert (r.status_code, r.json()["detail"]) == (503, detail), (method, path)
                # the role checks still come first
                assert (await api.call(method, url, role=None)).status_code == 401
                if (method, path) in ADMIN_ROUTES:
                    assert (await api.call(method, url, role=DEV)).status_code == 403


# ---- the client secret is write-only ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_client_secret_never_leaves_the_server(crm_api: Api, crm_env: CrmEnv, caplog):
    crm_env.settings.audit_enabled = True
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        created = await crm_api.call("POST", f"{BASE}/sources", json=source_body())
        assert created.status_code == 201
        body = created.json()
        source_id = body["id"]
        assert "client_secret" not in body and body["client_secret_set"] is True
        assert body["client_secret_hint"] == "…" + SECRET[-4:] and body["credentials_readable"] is True
        assert (body["tenant_id"], body["client_id"]) == (TENANT_ID, CLIENT_ID)  # identifiers are shown to SA/DEV
        texts.append(created.text)
        for role in (SA, DEV):
            texts.append((await crm_api.call("GET", f"{BASE}/sources", role=role)).text)
            texts.append((await crm_api.call("GET", f"{BASE}/sources/{source_id}", role=role)).text)
            texts.append((await crm_api.call("POST", f"{BASE}/sources/{source_id}/test", role=role)).text)
        rotated = await crm_api.call("PATCH", f"{BASE}/sources/{source_id}", json={"client_secret": NEW_SECRET})
        assert rotated.status_code == 200 and rotated.json()["client_secret_hint"] == "…" + NEW_SECRET[-4:]
        texts.append(rotated.text)
        # Validation errors never echo the submitted values (FastAPI's default 422 would).
        missing = await crm_api.call("POST", f"{BASE}/sources", json={**source_body(), "site_url": None})
        too_long = await crm_api.call("PATCH", f"{BASE}/sources/{source_id}", json={"client_secret": SECRET * 30})
        not_json = await crm_api.call("POST", f"{BASE}/sources", content=f"client_secret={SECRET}",
                                      headers={"content-type": "application/json"})
        for response in (missing, too_long, not_json):
            assert response.status_code == 422, response.text
            assert all(set(e) == {"type", "loc", "msg"} for e in response.json()["detail"])
            texts.append(response.text)
        assert missing.json()["detail"][0]["loc"] == ["body", "site_url"]
        assert too_long.json()["detail"][0]["loc"] == ["body", "client_secret"]
    for text in texts:
        assert SECRET not in text and NEW_SECRET not in text
    assert SECRET not in caplog.text and NEW_SECRET not in caplog.text
    audits = json.dumps(crm_env.audits, default=str, ensure_ascii=False)
    assert SECRET not in audits and NEW_SECRET not in audits and TENANT_ID not in audits and CLIENT_ID not in audits
    assert [(a["action_type"], a["new_value"].get("client_secret")) for a in crm_env.audits] == [("CREATE", "set"), ("UPDATE", "rotated")]
    stored = json.dumps(crm_env.repo.sources)
    assert SECRET not in stored and NEW_SECRET not in stored


@pytest.mark.asyncio
async def test_a_missing_encryption_key_blocks_only_credential_writes(crm_api: Api, crm_env: CrmEnv):
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    crm_env.key_missing = True
    r = await crm_api.call("POST", f"{BASE}/sources", json=source_body(name="Another"))
    assert (r.status_code, r.json()["detail"]) == (503, KEY_MISSING_REASON)
    r = await crm_api.call("PATCH", f"{BASE}/sources/{source_id}", json={"client_secret": NEW_SECRET})
    assert (r.status_code, r.json()["detail"]) == (503, KEY_MISSING_REASON)
    r = await crm_api.call("PATCH", f"{BASE}/sources/{source_id}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    listing = await crm_api.call("GET", f"{BASE}/sources", role=DEV)
    assert listing.status_code == 200
    (item,) = listing.json()
    assert item["credentials_readable"] is False and item["tenant_id"] is None and item["client_secret_set"] is True
    test = (await crm_api.call("POST", f"{BASE}/sources/{source_id}/test", role=DEV)).json()
    assert test["ok"] is False and test["steps"][0]["key"] == "credentials" and test["steps"][0]["detail"] == KEY_MISSING_REASON


# ---- behaviour ---------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_sync_now_is_409(crm_api: Api):
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    first = await crm_api.call("POST", f"{BASE}/sources/{source_id}/sync")
    assert first.status_code == 202 and first.json()["status"] == "QUEUED" and first.json()["trigger_type"] == "MANUAL"
    second = await crm_api.call("POST", f"{BASE}/sources/{source_id}/sync")
    assert (second.status_code, second.json()["detail"]) == (409, "A sync is already queued or running for this source")
    source = (await crm_api.call("GET", f"{BASE}/sources/{source_id}", role=DEV)).json()
    assert source["active_run"]["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_create_update_and_delete_errors_are_explicit(crm_api: Api):
    r = await crm_api.call("POST", f"{BASE}/sources", json=source_body(file_extensions=[".xlsx"]))
    assert (r.status_code, r.json()["detail"]) == (400, "Unsupported file type: .xlsx (only .pdf, .docx can be processed)")
    r = await crm_api.call("POST", f"{BASE}/sources", json=source_body(site_url="https://example.com/sites/x"))
    assert r.status_code == 400 and "SharePoint Online" in r.json()["detail"]
    r = await crm_api.call("POST", f"{BASE}/sources", json=source_body(account_id=999))
    assert (r.status_code, r.json()["detail"]) == (404, "Account not found")
    r = await crm_api.call("POST", f"{BASE}/sources", json=source_body(sync_interval_days=0))
    assert r.status_code == 422
    for method in ("GET", "PATCH", "DELETE"):
        r = await crm_api.call(method, f"{BASE}/sources/999", **({"json": {"name": "x"}} if method == "PATCH" else {}))
        assert (r.status_code, r.json()["detail"]) == (404, "Source not found"), method
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    deleted = await crm_api.call("DELETE", f"{BASE}/sources/{source_id}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert (await crm_api.call("GET", f"{BASE}/sources/{source_id}", role=DEV)).status_code == 404
    assert (await crm_api.call("POST", f"{BASE}/sources/{source_id}/sync")).status_code == 404


@pytest.mark.asyncio
async def test_runs_files_retry_and_entities(crm_api: Api, crm_env: CrmEnv):
    ids = await seeded(crm_api, crm_env)
    source_id = ids["source_id"]
    runs = (await crm_api.call("GET", f"{BASE}/sources/{source_id}/runs", role=DEV, params={"limit": 5})).json()
    assert runs["total"] == 1 and runs["limit"] == 5 and runs["items"][0]["status"] == "PARTIAL"
    assert runs["items"][0]["files_failed"] == 1 and runs["items"][0]["error_message"] == "1 of 2 files failed"

    files = (await crm_api.call("GET", f"{BASE}/sources/{source_id}/files", role=DEV, params={"status": "FAILED"})).json()
    (failed,) = files["items"]
    assert failed["name"] == "b.pdf" and failed["failed_stage"] == "extraction"
    assert [s["name"] for s in failed["stages"]] == ["download", "extraction", "intelligence", "entities", "persist"]
    assert [s["status"] for s in failed["stages"]] == ["COMPLETED", "FAILED", "PENDING", "PENDING", "PENDING"]
    assert failed["stages"][1]["error"] == "File is corrupt"
    assert (await crm_api.call("GET", f"{BASE}/sources/{source_id}/files", params={"state": "BOGUS"})).status_code == 422
    assert (await crm_api.call("GET", f"{BASE}/sources/{source_id}/files", params={"limit": 201})).status_code == 422

    retried = await crm_api.call("POST", f"{BASE}/source-files/{ids['file_id']}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "PENDING" and retried.json()["attempts"] == 0
    done = crm_env.file_by_item("a")["id"]
    r = await crm_api.call("POST", f"{BASE}/source-files/{done}/retry")
    assert (r.status_code, r.json()["detail"]) == (409, "Only failed files can be retried (this one is COMPLETED)")
    assert (await crm_api.call("POST", f"{BASE}/source-files/99999/retry")).status_code == 404

    entities = (await crm_api.call("GET", f"{BASE}/crm/entities", params={"source_id": source_id, "q": "acme"})).json()
    (org,) = entities["items"]
    assert org["entity_type"] == "organization" and org["display_value"] == "Acme Trading a" and org["file_name"] == "a.pdf"
    assert org["fields"] == {"name": "Acme Trading a", "tax_id": "123-456-789"} and org["provenance"]["entity_type"] == "organization"
    one = (await crm_api.call("GET", f"{BASE}/crm/entities/{ids['entity_id']}")).json()
    assert one["id"] == ids["entity_id"] and one["status"] == "ACTIVE" and one["source_name"] == "Sales contracts"
    for params in ({"limit": 0}, {"limit": 201}, {"status": "BOGUS"}, {"offset": -1}):
        assert (await crm_api.call("GET", f"{BASE}/crm/entities", params=params)).status_code == 422, params
    r = await crm_api.call("GET", f"{BASE}/crm/entities/99999")
    assert (r.status_code, r.json()["detail"]) == (404, "Entity not found")


def test_the_source_bodies_stay_documented_in_openapi(crm_api: Api):
    paths = crm_api.app.openapi()["paths"]
    for path, method, required in ((f"{BASE}/sources", "post", True), (f"{BASE}/sources/{{source_id}}", "patch", False)):
        schema = paths[path][method]["requestBody"]["content"]["application/json"]["schema"]
        secret = schema["properties"]["client_secret"]
        assert secret.get("writeOnly") is True or any(s.get("writeOnly") for s in secret.get("anyOf", []))
        assert ("client_secret" in schema.get("required", [])) is required
    assert paths[f"{BASE}/sources/{{source_id}}"]["delete"]["responses"].keys() >= {"204"}


@pytest.mark.asyncio
async def test_only_a_super_admin_sees_the_secret_hint(crm_api: Api):
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    for path in (f"{BASE}/sources/{source_id}", f"{BASE}/sources"):
        admin = (await crm_api.call("GET", path, role=SA)).json()
        developer = (await crm_api.call("GET", path, role=DEV)).json()
        admin, developer = (admin[0], developer[0]) if isinstance(admin, list) else (admin, developer)
        assert admin["client_secret_hint"] == "…" + SECRET[-4:]
        assert developer["client_secret_hint"] is None
        assert developer["client_secret_set"] is True and developer["secret_updated_at"] == admin["secret_updated_at"]
        assert (developer["tenant_id"], developer["client_id"]) == (TENANT_ID, CLIENT_ID)


@pytest.mark.asyncio
async def test_patch_clears_explicit_nulls_and_keeps_omitted_fields(crm_api: Api, crm_env: CrmEnv):
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    before = dict(crm_env.source(source_id))
    crm_env.source(source_id).update(resolved_site_id="s", resolved_drive_id="d", resolved_folder_id="f")
    # Omitted fields and a null secret keep their values.
    kept = await crm_api.call("PATCH", f"{BASE}/sources/{source_id}", json={"name": "Renamed", "client_secret": None})
    assert kept.status_code == 200
    row = crm_env.source(source_id)
    assert (row["account_id"], row["drive_name"], row["folder_path"]) == (3, "Documents", "/CRM/Contracts")
    assert row["client_secret_enc"] == before["client_secret_enc"] and row["resolved_folder_id"] == "f"
    # Explicit nulls clear the account, the library (the default one) and the folder (the root).
    cleared = await crm_api.call(
        "PATCH", f"{BASE}/sources/{source_id}", json={"account_id": None, "drive_name": None, "folder_path": None}
    )
    body = cleared.json()
    assert cleared.status_code == 200 and (body["account_id"], body["drive_name"], body["folder_path"]) == (None, None, None)
    row = crm_env.source(source_id)
    assert (row["resolved_site_id"], row["resolved_drive_id"], row["resolved_folder_id"]) == (None, None, None)
    assert row["client_secret_enc"] == before["client_secret_enc"]


@pytest.mark.asyncio
async def test_retry_delete_and_sync_now_rules(crm_api: Api, crm_env: CrmEnv):
    ids = await seeded(crm_api, crm_env)
    source_id, file_id = ids["source_id"], ids["file_id"]
    started = await crm_api.call("POST", f"{BASE}/sources/{source_id}/sync")
    await crm_env.repo.claim_next_run("w")  # that run is RUNNING now
    retried = await crm_api.call("POST", f"{BASE}/source-files/{file_id}/retry")
    assert retried.status_code == 202 and retried.json()["status"] == "PENDING"  # reset; the next run takes it
    assert [r["id"] for r in crm_env.repo.runs.values() if r["status"] in ("QUEUED", "RUNNING")] == [started.json()["id"]]

    blocked = await crm_api.call("DELETE", f"{BASE}/sources/{source_id}")
    assert (blocked.status_code, blocked.json()["detail"]) == (
        409, "A sync is queued or running for this source — delete it once the sync has finished",
    )
    crm_env.repo.runs[started.json()["id"]]["status"] = "COMPLETED"
    assert (await crm_api.call("DELETE", f"{BASE}/sources/{source_id}")).status_code == 204
    assert all(e["status"] == "WITHDRAWN" for e in crm_env.repo.entities.values() if e["source_id"] == source_id)
    assert (await crm_api.call("POST", f"{BASE}/sources/{source_id}/test", role=DEV)).status_code == 404
    withdrawn = (await crm_api.call("GET", f"{BASE}/crm/entities", params={"status": "WITHDRAWN"})).json()
    assert withdrawn["total"] == 2 and {e["source_name"] for e in withdrawn["items"]} == {"Sales contracts (deleted)"}

    spare = ids["spare_id"]
    await crm_api.call("PATCH", f"{BASE}/sources/{spare}", json={"status": "DISABLED"})
    r = await crm_api.call("POST", f"{BASE}/sources/{spare}/sync")
    assert (r.status_code, r.json()["detail"]) == (409, "This source is disabled — enable it first")


@pytest.mark.asyncio
async def test_status_reports_the_phase2_state(crm_env: CrmEnv, monkeypatch):
    from backend.doc_intel import crypto

    seen: list[object] = []

    def configured(settings) -> bool:
        seen.append(settings)
        return True

    monkeypatch.setattr(crypto, "secrets_configured", configured)
    crm_env.settings.scheduler_enabled = True
    async with api_client(crm_env.env, crm_env.runtime()) as api:
        body = (await api.call("GET", f"{BASE}/status", role=DEV)).json()
        assert (body["crm_installed"], body["sync_worker_running"], body["scheduler_enabled"], body["secrets_key_configured"]) == (
            True, True, True, True,
        )
        assert body["installed"] is True and body["detail"] is None and seen == [crm_env.settings]

        def broken(settings) -> bool:
            raise RuntimeError("boom")

        monkeypatch.setattr(crypto, "secrets_configured", broken)
        assert (await api.call("GET", f"{BASE}/status")).json()["secrets_key_configured"] is False
    async with api_client(crm_env.env, crm_env.runtime(crm_installed=False, sync_service=None, sync_worker=None)) as api:
        body = (await api.call("GET", f"{BASE}/status")).json()
        assert (body["crm_installed"], body["sync_worker_running"]) == (False, False) and body["detail"] is None


@pytest.mark.asyncio
async def test_the_connection_test_is_a_read_only_diagnostic(crm_api: Api, crm_env: CrmEnv):
    source_id = (await crm_api.call("POST", f"{BASE}/sources", json=source_body())).json()["id"]
    r = await crm_api.call("POST", f"{BASE}/sources/{source_id}/test", role=DEV)
    body = r.json()
    assert r.status_code == 200 and body["ok"] is True and body["files_found"] == 2
    assert [s["key"] for s in body["steps"]] == ["credentials", "token", "site", "drive", "folder", "listing"]
    assert set(body["steps"][0]) == {"key", "label", "ok", "latency_ms", "detail", "suggested_action"}
    assert crm_env.repo.runs == {} and crm_env.repo.files == {}
