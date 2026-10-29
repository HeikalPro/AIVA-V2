"""Regression tests for the lead's post-review fixes (plan review findings F4, F9, F12, F13)."""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from backend.auth.role_constants import ROLE_AGENT, ROLE_SUPER_ADMIN
from backend.doc_intel import extraction, kb_import
from backend.doc_intel.settings import DocIntelSettings

from .conftest import ACCOUNT_ID, PDF_BYTES, Api

KB = "/api/doc-intel/kb-documents"


# ---- F9: the upload body is parsed only after the Super Admin guard ------------------------


def _parts(account_id: object = ACCOUNT_ID, queue_keys=("HALAN",)) -> dict:
    return {
        "data": {"account_id": str(account_id), "queue_keys": list(queue_keys)},
        "files": [("files", ("a.pdf", PDF_BYTES, "application/pdf"))],
    }


@pytest.mark.asyncio
async def test_upload_body_is_not_read_before_the_guard(api: Api, monkeypatch):
    """Anonymous and non-admin callers are refused without the form ever being parsed."""
    from starlette.requests import Request

    parsed: list[int] = []
    original = Request.form

    def spying_form(self, *args, **kwargs):
        parsed.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spying_form)
    assert (await api.call("POST", KB, role=None, **_parts())).status_code == 401
    assert (await api.call("POST", KB, role=ROLE_AGENT, **_parts())).status_code == 403
    assert parsed == []
    ok = await api.call("POST", KB, role=ROLE_SUPER_ADMIN, **_parts())
    assert ok.status_code == 202, ok.text
    assert parsed == [1]


@pytest.mark.asyncio
async def test_upload_rejects_an_oversized_declared_body_with_413(api: Api):
    settings = api.env.settings
    too_big = settings.max_upload_bytes * settings.max_files_per_upload + 2 * 1024 * 1024
    r = await api.call(
        "POST", KB, headers={"content-length": str(too_big), "content-type": "multipart/form-data; boundary=x"},
        content=b"--x--\r\n",
    )
    assert r.status_code == 413
    assert "Upload too large" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("account_id", ["", "abc", "1.5"])
async def test_upload_requires_a_numeric_account_id(api: Api, account_id):
    r = await api.call("POST", KB, **_parts(account_id=account_id))
    assert r.status_code == 400
    assert "account_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_still_validates_queues_and_files(api: Api):
    no_queue = await api.call("POST", KB, data={"account_id": str(ACCOUNT_ID)},
                              files=[("files", ("a.pdf", PDF_BYTES, "application/pdf"))])
    assert no_queue.status_code == 400 and "queue" in no_queue.json()["detail"].lower()
    no_file = await api.call("POST", KB, data={"account_id": str(ACCOUNT_ID), "queue_keys": ["HALAN"]})
    assert no_file.status_code == 400 and "file" in no_file.json()["detail"].lower()


def test_upload_route_keeps_a_documented_multipart_body(api: Api):
    spec = api.app.openapi()["paths"][KB]["post"]
    schema = spec["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert set(schema["required"]) == {"account_id", "queue_keys", "files"}


# ---- F4: doc-intel failures never land org-less in AIVA_error_logs -------------------------


def _service(tmp_path) -> kb_import.KbImportService:
    settings = DocIntelSettings(_env_file=None, storage_dir=str(tmp_path))
    return kb_import.KbImportService(db=None, repo=None, kb=None, settings=settings, embedder_factory=lambda c: None)


@pytest.mark.asyncio
async def test_error_log_is_skipped_without_a_platform_org(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_persist(db, **kw):
        calls.append(kw)

    monkeypatch.setattr(kb_import, "persist_error_log", fake_persist)
    monkeypatch.setattr(kb_import, "get_backend_settings", lambda: SimpleNamespace(notify_platform_org_id=None))
    await _service(tmp_path)._write_error_log(exception_type="X", exception_message="m", stack_trace="t", path="p")
    assert calls == []


@pytest.mark.asyncio
async def test_error_log_is_scoped_to_the_platform_org(tmp_path, monkeypatch):
    calls: list[dict] = []

    async def fake_persist(db, **kw):
        calls.append(kw)

    monkeypatch.setattr(kb_import, "persist_error_log", fake_persist)
    monkeypatch.setattr(kb_import, "get_backend_settings", lambda: SimpleNamespace(notify_platform_org_id=2))
    await _service(tmp_path)._write_error_log(exception_type="X", exception_message="m", stack_trace="t", path="p")
    assert len(calls) == 1 and calls[0]["org_id"] == 2 and calls[0]["source"] == "DOC_INTEL"


# ---- F12: shutdown kills an in-flight extraction child -------------------------------------


def test_terminate_active_extractions_kills_a_running_child(tmp_path):
    outcome: dict = {}

    def run() -> None:
        started = time.perf_counter()
        try:
            result = extraction._spawn(
                [sys.executable, "-c", "import time; time.sleep(60)"], cwd=tmp_path, env=None, timeout=120
            )
            outcome["returncode"] = result.returncode
        except subprocess.TimeoutExpired:
            outcome["returncode"] = "timeout"
        outcome["seconds"] = time.perf_counter() - started

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.time() + 10
    while not extraction._ACTIVE_CHILDREN and time.time() < deadline:
        time.sleep(0.05)
    assert extraction._ACTIVE_CHILDREN, "child never registered"
    assert extraction.terminate_active_extractions() == 1
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert outcome["returncode"] not in (0, "timeout")
    assert outcome["seconds"] < 20
    assert not extraction._ACTIVE_CHILDREN


def test_terminate_active_extractions_with_nothing_running():
    assert extraction.terminate_active_extractions() == 0


@pytest.mark.asyncio
async def test_stop_doc_intel_kills_extractions_before_stopping_the_worker():
    from backend.doc_intel.runtime import DocIntelRuntime, stop_doc_intel

    order: list[str] = []

    class Worker:
        running = True

        async def stop(self) -> None:
            order.append("worker.stop")

    runtime = DocIntelRuntime(settings=DocIntelSettings(_env_file=None))
    runtime.worker = Worker()
    runtime.extraction = SimpleNamespace(terminate_active_extractions=lambda: order.append("kill") or 1)
    await stop_doc_intel(runtime)
    assert order == ["kill", "worker.stop"]


# ---- F13: "Run checks now" fits under nginx's 60 s proxy timeout by default ----------------


def test_default_smoke_timeout_fits_the_proxy_timeout():
    settings = DocIntelSettings(_env_file=None)
    assert settings.extraction_smoke_timeout_seconds + 10 < 60
