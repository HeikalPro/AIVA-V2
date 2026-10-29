"""HTTP API: /api/doc-intel/kb-documents (Super Admin only), through a test app with only this router."""
from __future__ import annotations

import pytest

from backend.auth.role_constants import ROLE_DEVELOPER, ROLE_SUPER_ADMIN
from backend.doc_intel.runtime import NOT_INSTALLED_DETAIL, NOT_STARTED_DETAIL

from .conftest import (
    ACCOUNT_ID,
    NON_PLATFORM_ROLES,
    PDF_BYTES,
    Api,
    api_client,
    doc_intel_routes,
    make_runtime,
)

KB = "/api/doc-intel/kb-documents"
SA = ROLE_SUPER_ADMIN


def upload_parts(*files: tuple[str, bytes, str], queue_keys=("HALAN",), account_id=ACCOUNT_ID) -> dict:
    return {
        "data": {"account_id": str(account_id), "queue_keys": list(queue_keys)},
        "files": [("files", f) for f in files],
    }


def request_kwargs(method: str, path: str, n: int = 0) -> dict:
    if method == "POST" and path == KB:
        return upload_parts((f"matrix-{n}.pdf", PDF_BYTES + str(n).encode(), "application/pdf"))
    if method == "PATCH" and path.endswith("/queues"):
        return {"json": {"queue_keys": ["HALAN"]}}
    return {}


async def published(api: Api, name: str = "a.pdf", variant: bytes = b"") -> int:
    r = await api.call("POST", KB, **upload_parts((name, PDF_BYTES + variant, "application/pdf")))
    assert r.status_code == 202, r.text
    assert await api.env.process_next() == "PUBLISHED"
    return r.json()["documents"][0]["id"]


@pytest.mark.asyncio
async def test_role_matrix_for_every_kb_document_route(api: Api):
    doc_id = await published(api)
    routes = [(m, p) for m, p, _ in doc_intel_routes(api.app) if p.startswith(KB)]
    assert len(routes) == 8, routes  # a new route must be added to this matrix deliberately
    for n, (method, path) in enumerate(routes):
        url = path.replace("{document_id}", str(doc_id))
        anonymous = await api.call(method, url, role=None, **request_kwargs(method, path, n))
        assert anonymous.status_code == 401, (method, path)
        for role in (*NON_PLATFORM_ROLES, ROLE_DEVELOPER):
            r = await api.call(method, url, role=role, **request_kwargs(method, path, n))
            assert r.status_code == 403, (method, path, role, r.text)
            assert r.json()["detail"] == "Requires one of roles: SUPER_ADMIN"
        allowed = await api.call(method, url, role=SA, **request_kwargs(method, path, n))
        assert allowed.status_code in (200, 202, 409), (method, path, allowed.status_code, allowed.text)


@pytest.mark.asyncio
async def test_upload_endpoint(api: Api):
    r = await api.call(
        "POST",
        KB,
        **upload_parts(
            ("Card FAQ.pdf", PDF_BYTES, "application/pdf"),
            ("notes.txt", b"plain text", "text/plain"),
            queue_keys=("HALAN", "Gomla"),
        ),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert (body["accepted"], body["rejected"]) == (1, 1) and len(body["batch_id"]) == 32
    good, bad = body["documents"]
    assert good["status"] == "QUEUED" and good["queue_keys"] == ["HALAN", "Gomla"]
    assert good["queue_labels"] == ["Halan", "Gomla"] and good["queue_position"] == 1
    assert good["account_name"] == "Hallan" and good["filename"] == "Card FAQ.pdf"
    assert [s["name"] for s in good["stages"]] == ["upload", "extraction", "chunking", "embedding", "publishing"]
    assert good["stages"][0]["status"] == "COMPLETED" and good["stages"][0]["finished_at"].endswith("Z")
    assert bad["status"] == "FAILED" and bad["failed_stage"] == "upload"
    assert bad["stages"][0] == {
        "name": "upload",
        "status": "FAILED",
        "error": "Unsupported file type (only PDF and DOCX)",
        "started_at": bad["stages"][0]["started_at"],
        "finished_at": bad["stages"][0]["finished_at"],
    }
    assert [s["status"] for s in bad["stages"][1:]] == ["SKIPPED"] * 4
    assert api.env.wakes == [1]
    assert await api.env.process_next() == "PUBLISHED"


@pytest.mark.asyncio
async def test_upload_validation_errors(api: Api):
    r = await api.call("POST", KB, **upload_parts(("a.pdf", PDF_BYTES, "application/pdf"), account_id=999))
    assert (r.status_code, r.json()["detail"]) == (404, "Account not found")
    r = await api.call("POST", KB, **upload_parts(("a.pdf", PDF_BYTES, "application/pdf"), queue_keys=("Nope",)))
    assert (r.status_code, r.json()["detail"]) == (400, "Unknown queue: Nope")
    # The form is parsed after the guard (review finding F9), so missing parts get the
    # service's explicit 400 messages instead of FastAPI's generic 422 validation error.
    r = await api.call("POST", KB, data={"account_id": str(ACCOUNT_ID), "queue_keys": ["HALAN"]})
    assert (r.status_code, r.json()["detail"]) == (400, "Select at least one file")
    r = await api.call("POST", KB, files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))], data={"account_id": "3"})
    assert (r.status_code, r.json()["detail"]) == (400, "Select at least one queue")


@pytest.mark.asyncio
async def test_list_get_and_filters(api: Api):
    doc_id = await published(api)
    r = await api.call("GET", KB, params={"account_id": ACCOUNT_ID, "status": "PUBLISHED", "limit": 10, "offset": 0})
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["limit"], body["offset"]) == (1, 10, 0)
    assert body["items"][0]["id"] == doc_id and body["items"][0]["status"] == "PUBLISHED"
    assert (await api.call("GET", KB, params={"status": "BOGUS"})).status_code == 422
    assert (await api.call("GET", KB, params={"limit": 201})).status_code == 422
    item = (await api.call("GET", f"{KB}/{doc_id}")).json()
    assert item["chunk_count"] > 0 and item["published_at"].endswith("Z") and item["tokens_used"] > 0
    missing = await api.call("GET", f"{KB}/99999")
    assert (missing.status_code, missing.json()["detail"]) == (404, "Document not found")


@pytest.mark.asyncio
async def test_preview_retry_republish_queues_and_unpublish(api: Api):
    doc_id = await published(api)
    preview = await api.call("GET", f"{KB}/{doc_id}/preview", params={"max_chars": 40})
    assert preview.status_code == 200
    assert preview.json()["truncated"] is True and preview.json()["pages"][0]["number"] == 1

    retry = await api.call("POST", f"{KB}/{doc_id}/retry")
    assert (retry.status_code, retry.json()["detail"]) == (409, "Only failed documents can be retried (this one is PUBLISHED)")

    queues = await api.call("PATCH", f"{KB}/{doc_id}/queues", json={"queue_keys": ["Gomla", "Cards"]})
    assert queues.status_code == 200 and queues.json()["queue_labels"] == ["Gomla", "Card Support"]
    assert (await api.call("PATCH", f"{KB}/{doc_id}/queues", json={"queue_keys": []})).status_code == 422

    republish = await api.call("POST", f"{KB}/{doc_id}/republish")
    assert republish.status_code == 202 and republish.json()["status"] == "QUEUED"
    conflict = await api.call("DELETE", f"{KB}/{doc_id}")
    assert conflict.status_code == 409
    assert await api.env.process_next() == "PUBLISHED"

    deleted = await api.call("DELETE", f"{KB}/{doc_id}")
    assert deleted.status_code == 200 and deleted.json()["status"] == "UNPUBLISHED"
    assert api.env.kb.queues_of(f"kbdoc-{doc_id}") == []


@pytest.mark.asyncio
async def test_routes_answer_503_until_installed(env):
    async with api_client(env, make_runtime(env, installed=False, service=None, health=None)) as api:
        r = await api.call("GET", KB)
        assert (r.status_code, r.json()["detail"]) == (503, NOT_INSTALLED_DETAIL)
        # role checks still come first
        assert (await api.call("GET", KB, role=ROLE_DEVELOPER)).status_code == 403
        assert (await api.call("GET", KB, role=None)).status_code == 401
    async with api_client(env, None) as api:
        r = await api.call("GET", KB)
        assert (r.status_code, r.json()["detail"]) == (503, NOT_STARTED_DETAIL)
