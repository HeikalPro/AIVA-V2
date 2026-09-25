"""Flow 1 state machine: upload, the five stages, failures per stage, admin actions, the worker."""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import threading
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from backend.doc_intel.extraction import ExtractionFailed
from backend.doc_intel.kb_import import KbImportWorker
from backend.doc_intel.kb_repo import INTERRUPTED_REASON, loads_json
from backend.doc_intel.kb_store import PublishFailed
from backend.doc_intel.normalized import NormalizedDocument, NormalizedPage
from backend.doc_intel.runtime import ServiceUnavailableError
from backend.doc_intel.textutil import utc_now
from backend.exceptions import BadRequestError, ConflictError, NotFoundError

from .conftest import ACCOUNT_ID, CORPUS_ID, PDF_BYTES, Env, FakeEmbedder, FakeUpload, make_user

SA = make_user("SUPER_ADMIN")


def pdf(n: int = 0) -> bytes:
    return PDF_BYTES + f"% variant {n}\n".encode()


async def upload_one(env: Env, *, name: str = "Card FAQ.pdf", data: bytes | None = None, keys=None) -> int:
    out = await env.upload((name, data if data is not None else pdf()), queue_keys=keys)
    return out.documents[0].id


async def published_doc(env: Env, **kw) -> int:
    doc_id = await upload_one(env, **kw)
    assert await env.process_next() == "PUBLISHED"
    return doc_id


def http_401() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://embed.example.test/v1/embeddings")
    return httpx.HTTPStatusError("401", request=request, response=httpx.Response(401, request=request))


def assert_stages(env: Env, doc_id: int, expected: dict[str, str]) -> None:
    assert env.stage_statuses(doc_id) == expected


# ---- success path --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_success_path(env: Env):
    out = await env.upload(("Card FAQ.pdf", pdf()), queue_keys=["HALAN"])
    assert (out.accepted, out.rejected) == (1, 0)
    doc = out.documents[0]
    assert doc.status == "QUEUED" and doc.queue_position == 1
    assert [s.status for s in doc.stages] == ["COMPLETED", "PENDING", "PENDING", "PENDING", "PENDING"]
    assert doc.queue_labels == ["Halan"] and doc.vertical == f"kbdoc-{doc.id}"
    assert env.wakes == [1]

    assert await env.process_next() == "PUBLISHED"
    row = env.row(doc.id)
    assert row["status"] == "PUBLISHED" and row["failed_stage"] is None
    assert_stages(env, doc.id, {s: "COMPLETED" for s in ("upload", "extraction", "chunking", "embedding", "publishing")})
    stored = env.kb.chunks[(CORPUS_ID, doc.vertical)]
    assert row["chunk_count"] == len(stored) > 0
    assert all(len(c["vector"]) == 64 for c in stored)
    assert stored[0]["text"].startswith("[Document: Card FAQ.pdf · Section: Cards")
    assert env.kb.queues_of(doc.vertical) == ["HALAN"]
    assert env.kb.configs[CORPUS_ID]["custom_setting"] == {"keep": True}
    assert row["page_count"] == 2 and row["tokens_used"] > 0
    assert row["cost_usd"] == pytest.approx(round(row["tokens_used"] / 1_000_000 * 0.02, 6))
    assert json.loads(row["warnings_json"])[0]["code"] == "low_text"
    assert row["published_at"] and row["finished_at"]
    details = loads_json(row["stage_details"], {})
    for stage in ("extraction", "chunking", "embedding", "publishing"):
        assert details[stage]["started_at"].endswith("Z") and details[stage]["finished_at"].endswith("Z")
    assert details["embedding"]["metrics"]["tokens_source"] == "provider"
    assert details["chunking"]["metrics"]["chunks"] == len(stored)

    (call,) = env.extractor.calls
    assert call["media_type"] == "application/pdf" and call["out_path"].name == "normalized.json"
    assert call["source"].name == "original.pdf"

    api_doc = await env.service.get_document_out(doc.id)
    assert api_doc.status == "PUBLISHED" and api_doc.queue_position is None
    assert all(s.started_at and s.finished_at for s in api_doc.stages)
    assert api_doc.warnings[0].message == "Page 2 has little text"


# ---- a failure at each stage ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_at_extraction(env: Env):
    env.extractor.error = ExtractionFailed("PDF is password-protected")
    doc_id = await upload_one(env)
    assert await env.process_next() == "FAILED"
    row = env.row(doc_id)
    assert row["status"] == "FAILED" and row["failed_stage"] == "extraction"
    assert row["error_message"] == "PDF is password-protected"
    assert_stages(env, doc_id, {"upload": "COMPLETED", "extraction": "FAILED", "chunking": "PENDING", "embedding": "PENDING", "publishing": "PENDING"})
    out = await env.service.get_document_out(doc_id)
    assert out.stages[1].error == "PDF is password-protected"
    assert "publish" not in env.kb.calls


@pytest.mark.asyncio
async def test_failure_at_chunking(env: Env):
    env.extractor.document = NormalizedDocument(
        filename="x.pdf", media_type="application/pdf", sha256="0" * 64, page_count=1,
        pages=[NormalizedPage(number=1, text="   ")],
    )
    doc_id = await upload_one(env)
    assert await env.process_next() == "FAILED"
    row = env.row(doc_id)
    assert (row["failed_stage"], row["error_message"]) == ("chunking", "Document produced no text chunks")
    assert_stages(env, doc_id, {"upload": "COMPLETED", "extraction": "COMPLETED", "chunking": "FAILED", "embedding": "PENDING", "publishing": "PENDING"})


@pytest.mark.asyncio
async def test_failure_at_chunking_when_the_corpus_is_gone(env: Env):
    doc_id = await upload_one(env)
    del env.kb.configs[CORPUS_ID]
    assert await env.process_next() == "FAILED"
    assert (env.row(doc_id)["failed_stage"], env.row(doc_id)["error_message"]) == ("chunking", "Knowledge base corpus not found")


@pytest.mark.asyncio
async def test_failure_at_embedding(env: Env):
    env.embedder = FakeEmbedder(errors=[http_401()])
    doc_id = await upload_one(env)
    assert await env.process_next() == "FAILED"
    row = env.row(doc_id)
    assert row["failed_stage"] == "embedding"
    assert row["error_message"].startswith("Embedding provider rejected the API key (401)")
    assert_stages(env, doc_id, {"upload": "COMPLETED", "extraction": "COMPLETED", "chunking": "COMPLETED", "embedding": "FAILED", "publishing": "PENDING"})
    assert row["chunk_count"] > 0 and "publish" not in env.kb.calls


@pytest.mark.asyncio
async def test_failure_at_publishing(env: Env):
    env.kb.publish_error = PublishFailed("Queue 'HALAN' no longer exists in this knowledge base", code="unknown_queue")
    doc_id = await upload_one(env)
    assert await env.process_next() == "FAILED"
    row = env.row(doc_id)
    assert (row["failed_stage"], row["error_message"]) == ("publishing", "Queue 'HALAN' no longer exists in this knowledge base")
    assert_stages(env, doc_id, {"upload": "COMPLETED", "extraction": "COMPLETED", "chunking": "COMPLETED", "embedding": "COMPLETED", "publishing": "FAILED"})
    assert row["tokens_used"] > 0  # recorded even though publishing failed


@pytest.mark.asyncio
async def test_unexpected_error_is_recorded_and_logged(env: Env):
    env.settings.error_log_enabled = True
    env.extractor.error = RuntimeError("segfault in parser")
    doc_id = await upload_one(env)
    assert await env.process_next() == "FAILED"  # never raises
    row = env.row(doc_id)
    assert row["failed_stage"] == "extraction"
    assert row["error_message"] == "Unexpected error: RuntimeError: segfault in parser"
    (logged,) = env.errors
    assert logged["exception_type"] == "RuntimeError" and "segfault" in logged["stack_trace"]
    assert logged["path"] == f"/api/doc-intel/kb-documents/{doc_id}"


@pytest.mark.asyncio
async def test_unexpected_error_is_not_logged_when_disabled(env: Env):
    env.extractor.error = RuntimeError("boom")
    await upload_one(env)
    assert await env.process_next() == "FAILED"
    assert env.errors == []


@pytest.mark.asyncio
async def test_row_that_left_processing_is_abandoned(env: Env):
    doc_id = await upload_one(env)
    row = await env.repo.claim_next("w")
    env.repo.refuse_updates = True  # e.g. recovered as interrupted by another process
    assert await env.service.process_document(row) == "ABANDONED"
    assert env.extractor.calls == [] and "publish" not in env.kb.calls
    assert env.row(doc_id)["status"] == "PROCESSING"


# ---- upload ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_mixed_batch(env: Env):
    env.settings.audit_enabled = True
    out = await env.upload(("good.pdf", pdf(1)), ("notes.txt", b"hello"), ("again.pdf", pdf(1)), queue_keys=["HALAN", "Gomla"])
    assert (out.accepted, out.rejected) == (1, 2)
    good, bad, dup = out.documents
    assert {d.batch_id for d in out.documents} == {out.batch_id}
    assert good.status == "QUEUED" and good.queue_keys == ["HALAN", "Gomla"] and good.queue_labels == ["Halan", "Gomla"]
    assert bad.status == "FAILED" and bad.failed_stage == "upload"
    assert bad.error_message == "Unsupported file type (only PDF and DOCX)"
    assert [s.status for s in bad.stages] == ["FAILED", "SKIPPED", "SKIPPED", "SKIPPED", "SKIPPED"]
    assert bad.stages[0].error == "Unsupported file type (only PDF and DOCX)"
    assert dup.error_message == f"Duplicate of document #{good.id}, which is already imported — use Change queues instead"
    assert dup.sha256 == good.sha256 and dup.stages[1].status == "SKIPPED"
    assert len(env.storage.removed) == 2 and not any(p.exists() for p in env.storage.removed)
    assert Path(env.row(good.id)["storage_dir"]).joinpath("original.pdf").is_file()
    assert env.wakes == [1]
    (audit,) = env.audits
    assert audit["action_type"] == "UPLOAD" and audit["entity_id"] == out.batch_id
    assert [d["status"] for d in audit["new_value"]["documents"]] == ["QUEUED", "FAILED", "FAILED"]


@pytest.mark.asyncio
async def test_upload_through_the_real_storage_module(env: Env):
    """Integration with backend.doc_intel.storage (filesystem only, under a temp dir)."""
    from fastapi import UploadFile

    from backend.doc_intel import storage
    from backend.doc_intel.kb_import import KbImportService

    service = KbImportService(
        db=None, repo=env.repo, kb=env.kb, settings=env.settings,
        embedder_factory=lambda cfg: env.embedder, extract_fn=env.extractor, storage=storage,
    )
    uploads = [
        UploadFile(file=io.BytesIO(pdf(1)), filename="../../etc/Card FAQ.pdf"),
        UploadFile(file=io.BytesIO(b"plain text"), filename="notes.txt"),
        UploadFile(file=io.BytesIO(pdf(2)), filename="renamed.docx"),
    ]
    out = await service.upload(SA, ACCOUNT_ID, ["HALAN"], uploads)
    good, text, mismatch = out.documents
    assert (out.accepted, out.rejected) == (1, 2)
    assert good.status == "QUEUED" and "/" not in good.filename and ".." not in good.filename
    assert good.content_type == "application/pdf" and good.size_bytes == len(pdf(1))
    kb_root = env.settings.storage_path / "kb"
    stored = Path(env.row(good.id)["storage_dir"])
    assert stored.parent == kb_root and (stored / "original.pdf").is_file()
    for rejected in (text, mismatch):
        assert rejected.status == "FAILED" and rejected.stages[0].status == "FAILED" and rejected.error_message
    assert [p.name for p in kb_root.iterdir()] == [stored.name]  # rejected uploads leave nothing behind
    assert await env.process_next() == "PUBLISHED"  # the pipeline reads the stored original


@pytest.mark.asyncio
async def test_upload_of_only_rejected_files_does_not_wake_the_worker(env: Env):
    out = await env.upload(("x.exe", b"MZ"))
    assert (out.accepted, out.rejected) == (0, 1)
    assert env.wakes == []


@pytest.mark.asyncio
async def test_a_failed_document_does_not_block_a_new_upload_of_the_same_file(env: Env):
    env.extractor.error = ExtractionFailed("corrupt")
    await upload_one(env, data=pdf(5))
    await env.process_next()
    out = await env.upload(("same.pdf", pdf(5)))
    assert out.accepted == 1


@pytest.mark.asyncio
async def test_upload_validation(env: Env):
    service = env.service
    with pytest.raises(NotFoundError):
        await service.upload(SA, 999, ["HALAN"], [FakeUpload("a.pdf", pdf())])
    env.repo.add_account(4, corpus_id=None)
    with pytest.raises(BadRequestError, match="no knowledge base"):
        await service.upload(SA, 4, ["HALAN"], [FakeUpload("a.pdf", pdf())])
    env.repo.add_account(5, corpus_id="aa" * 16)
    with pytest.raises(BadRequestError, match="Knowledge base corpus not found"):
        await service.upload(SA, 5, ["HALAN"], [FakeUpload("a.pdf", pdf())])
    with pytest.raises(BadRequestError, match="Unknown queue: Nope"):
        await service.upload(SA, ACCOUNT_ID, ["HALAN", "Nope"], [FakeUpload("a.pdf", pdf())])
    with pytest.raises(BadRequestError, match="Select at least one queue"):
        await service.upload(SA, ACCOUNT_ID, [" "], [FakeUpload("a.pdf", pdf())])
    with pytest.raises(BadRequestError, match="Select at least one file"):
        await service.upload(SA, ACCOUNT_ID, ["HALAN"], [])
    too_many = [FakeUpload(f"{i}.pdf", pdf(i)) for i in range(env.settings.max_files_per_upload + 1)]
    with pytest.raises(BadRequestError, match="Too many files"):
        await service.upload(SA, ACCOUNT_ID, ["HALAN"], too_many)
    assert env.repo.rows == {}  # nothing stored for rejected requests


@pytest.mark.asyncio
async def test_upload_when_the_knowledge_base_database_is_down(env: Env):
    env.kb.config_error = RuntimeError("DPY-6005: cannot connect to database")
    with pytest.raises(ServiceUnavailableError) as info:
        await env.upload(("a.pdf", pdf()))
    assert info.value.status_code == 503
    assert info.value.detail == "Knowledge base database unavailable: DPY-6005: cannot connect to database"


# ---- retry / republish / unpublish / change queues -----------------------------------------


@pytest.mark.asyncio
async def test_retry_resumes_at_chunking_and_reuses_the_extracted_text(env: Env):
    env.embedder = FakeEmbedder(errors=[http_401()])
    doc_id = await upload_one(env)
    await env.process_next()
    out = await env.service.retry(SA, doc_id)
    assert out.status == "QUEUED" and out.failed_stage is None and out.error_message is None
    assert [s.status for s in out.stages] == ["COMPLETED", "COMPLETED", "PENDING", "PENDING", "PENDING"]
    env.embedder = FakeEmbedder()
    assert await env.process_next() == "PUBLISHED"
    assert len(env.extractor.calls) == 1  # extraction was not repeated
    assert env.row(doc_id)["attempts"] == 2


@pytest.mark.asyncio
async def test_retry_after_failed_extraction_starts_at_extraction(env: Env):
    env.extractor.error = ExtractionFailed("timed out")
    doc_id = await upload_one(env)
    await env.process_next()
    out = await env.service.retry(SA, doc_id)
    assert [s.status for s in out.stages] == ["COMPLETED", "PENDING", "PENDING", "PENDING", "PENDING"]
    env.extractor.error = None
    assert await env.process_next() == "PUBLISHED"
    assert len(env.extractor.calls) == 2


@pytest.mark.asyncio
async def test_retry_rules(env: Env):
    published = await published_doc(env, data=pdf(1))
    with pytest.raises(ConflictError, match=r"Only failed documents can be retried \(this one is PUBLISHED\)"):
        await env.service.retry(SA, published)
    rejected = (await env.upload(("x.txt", b"x"))).documents[0].id
    with pytest.raises(ConflictError, match="The upload failed; upload the file again"):
        await env.service.retry(SA, rejected)
    env.extractor.error = ExtractionFailed("corrupt")
    gone = await upload_one(env, data=pdf(2))
    await env.process_next()
    shutil.rmtree(env.row(gone)["storage_dir"])
    with pytest.raises(ConflictError, match="no longer on the server"):
        await env.service.retry(SA, gone)
    with pytest.raises(NotFoundError):
        await env.service.retry(SA, 12345)


@pytest.mark.asyncio
async def test_republish_rules(env: Env):
    doc_id = await published_doc(env)
    before = list(env.kb.chunks)
    out = await env.service.republish(SA, doc_id)
    assert out.status == "QUEUED"
    assert [s.status for s in out.stages] == ["COMPLETED", "COMPLETED", "PENDING", "PENDING", "PENDING"]
    assert list(env.kb.chunks) == before  # still live until the new version replaces it
    assert await env.process_next() == "PUBLISHED"
    assert len(env.extractor.calls) == 1

    queued = await upload_one(env, data=pdf(9))
    with pytest.raises(ConflictError, match=r"can be republished \(this one is QUEUED\)"):
        await env.service.republish(SA, queued)

    env.extractor.error = ExtractionFailed("corrupt")
    await env.process_next()  # the queued document now fails at extraction
    assert env.row(queued)["failed_stage"] == "extraction"
    with pytest.raises(ConflictError, match=r"\(this one is FAILED\)"):
        await env.service.republish(SA, queued)


@pytest.mark.asyncio
async def test_republish_of_a_document_failed_after_extraction(env: Env):
    env.embedder = FakeEmbedder(errors=[http_401()])
    doc_id = await upload_one(env)
    await env.process_next()
    assert (await env.service.republish(SA, doc_id)).status == "QUEUED"
    env.embedder = FakeEmbedder()
    assert await env.process_next() == "PUBLISHED"


@pytest.mark.asyncio
async def test_republish_needs_the_extracted_text(env: Env):
    doc_id = await published_doc(env)
    (Path(env.row(doc_id)["storage_dir"]) / "normalized.json").unlink()
    with pytest.raises(ConflictError, match="extracted text is no longer on the server"):
        await env.service.republish(SA, doc_id)


@pytest.mark.asyncio
async def test_unpublish_rules(env: Env):
    env.settings.audit_enabled = True
    doc_id = await published_doc(env, keys=["HALAN", "Gomla"])
    vertical = f"kbdoc-{doc_id}"
    out = await env.service.unpublish(SA, doc_id)
    assert out.status == "UNPUBLISHED"
    assert env.kb.queues_of(vertical) == [] and (CORPUS_ID, vertical) not in env.kb.chunks
    audit = env.audits[-1]
    assert audit["action_type"] == "UNPUBLISH" and audit["new_value"]["removed_from_queues"] == ["Gomla", "HALAN"]

    calls = len(env.kb.calls)
    assert (await env.service.unpublish(SA, doc_id)).status == "UNPUBLISHED"  # idempotent
    assert len(env.kb.calls) == calls

    queued = await upload_one(env, data=pdf(4))
    with pytest.raises(ConflictError, match="still being imported"):
        await env.service.unpublish(SA, queued)
    await env.repo.claim_next("w")
    with pytest.raises(ConflictError, match="still being imported"):
        await env.service.unpublish(SA, queued)


@pytest.mark.asyncio
async def test_unpublish_a_failed_document(env: Env):
    env.kb.publish_error = PublishFailed("Knowledge base database error: ORA-03113", code="database")
    doc_id = await upload_one(env)
    await env.process_next()
    env.kb.publish_error = None
    assert (await env.service.unpublish(SA, doc_id)).status == "UNPUBLISHED"


@pytest.mark.asyncio
async def test_unpublish_when_the_knowledge_base_is_down(env: Env):
    doc_id = await published_doc(env)
    env.kb.publish_error = PublishFailed("Knowledge base database error: DPY-6005", code="database")
    with pytest.raises(ServiceUnavailableError):
        await env.service.unpublish(SA, doc_id)
    assert env.row(doc_id)["status"] == "PUBLISHED"


@pytest.mark.asyncio
async def test_change_queues_of_a_published_document_is_config_only(env: Env):
    env.settings.audit_enabled = True
    doc_id = await published_doc(env, keys=["HALAN"])
    embed_calls = len(env.embedder.calls)
    out = await env.service.change_queues(SA, doc_id, ["Gomla", "Cards"])
    assert out.queue_keys == ["Gomla", "Cards"] and out.queue_labels == ["Gomla", "Card Support"]
    assert env.kb.queues_of(f"kbdoc-{doc_id}") == ["Cards", "Gomla"]
    assert len(env.embedder.calls) == embed_calls  # no re-embedding
    assert out.status == "PUBLISHED"
    assert env.audits[-1]["old_value"] == {"queue_keys": ["HALAN"]}


@pytest.mark.asyncio
async def test_change_queues_of_queued_or_failed_documents_touches_the_row_only(env: Env):
    queued = await upload_one(env)
    out = await env.service.change_queues(SA, queued, ["Cards"])
    assert out.queue_keys == ["Cards"] and "set_queues" not in env.kb.calls
    await env.process_next()
    assert env.kb.queues_of(f"kbdoc-{queued}") == ["Cards"]  # published where the row said

    env.extractor.error = ExtractionFailed("corrupt")
    failed = await upload_one(env, data=pdf(2))
    await env.process_next()
    assert (await env.service.change_queues(SA, failed, ["Gomla"])).queue_keys == ["Gomla"]
    assert "set_queues" not in env.kb.calls


@pytest.mark.asyncio
async def test_change_queues_rules(env: Env):
    doc_id = await published_doc(env)
    with pytest.raises(BadRequestError, match="Unknown queue: Nope"):
        await env.service.change_queues(SA, doc_id, ["Nope"])
    with pytest.raises(BadRequestError, match="Select at least one queue"):
        await env.service.change_queues(SA, doc_id, [])
    await env.service.unpublish(SA, doc_id)
    with pytest.raises(ConflictError, match="unpublished"):
        await env.service.change_queues(SA, doc_id, ["HALAN"])
    processing = await upload_one(env, data=pdf(7))
    await env.repo.claim_next("w")
    with pytest.raises(ConflictError, match="being imported right now"):
        await env.service.change_queues(SA, processing, ["HALAN"])


@pytest.mark.asyncio
async def test_change_queues_maps_knowledge_base_failures(env: Env):
    doc_id = await published_doc(env)
    env.kb.publish_error = PublishFailed("The knowledge base configuration is locked by another operation", code="locked")
    with pytest.raises(ServiceUnavailableError):
        await env.service.change_queues(SA, doc_id, ["Gomla"])
    env.kb.publish_error = PublishFailed("Queue 'Gomla' no longer exists in this knowledge base", code="unknown_queue")
    with pytest.raises(ConflictError):
        await env.service.change_queues(SA, doc_id, ["Gomla"])


# ---- preview / list ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview(env: Env):
    doc_id = await upload_one(env)
    with pytest.raises(ConflictError, match="No extracted text yet"):
        await env.service.preview(doc_id, 1000)
    await env.process_next()
    full = await env.service.preview(doc_id, 100_000)
    assert [p.number for p in full.pages] == [1, 2] and not full.truncated
    assert full.page_count == 2 and full.extractor["pdf_engine"] == "pdfium"
    short = await env.service.preview(doc_id, 50)
    assert short.truncated and sum(len(p.text) for p in short.pages) == 50


@pytest.mark.asyncio
async def test_list_documents_with_positions_and_filters(env: Env):
    first = await upload_one(env, data=pdf(1))
    second = await upload_one(env, data=pdf(2))
    listing = await env.service.list_documents(limit=10, offset=0)
    assert listing.total == 2 and [d.id for d in listing.items] == [second, first]
    assert {d.id: d.queue_position for d in listing.items} == {first: 1, second: 2}
    assert (await env.service.list_documents(status="PUBLISHED")).total == 0
    assert (await env.service.list_documents(account_id=ACCOUNT_ID)).total == 2


@pytest.mark.asyncio
async def test_queue_labels_degrade_to_keys_when_the_knowledge_base_is_unreachable(env: Env):
    doc_id = await upload_one(env)
    env.service._config_cache.clear()
    env.kb.config_error = RuntimeError("down")
    out = await env.service.get_document_out(doc_id)
    assert out.queue_labels == ["HALAN"]


# ---- worker --------------------------------------------------------------------------------


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_worker_processes_uploads(env: Env):
    worker = KbImportWorker(env.service, env.settings, worker_id="test-worker")
    env.service.set_wake_callback(worker.wake)
    worker.start()
    try:
        assert worker.running
        doc_id = await upload_one(env)
        await wait_for(lambda: env.row(doc_id)["status"] == "PUBLISHED")
        assert env.row(doc_id)["worker_id"] == "test-worker"
    finally:
        await worker.stop()
    assert not worker.running
    threshold = env.repo.recover_calls[0]
    assert utc_now() - threshold >= timedelta(minutes=5) - timedelta(seconds=5)


@pytest.mark.asyncio
async def test_worker_survives_errors(env: Env):
    env.repo.claim_error = RuntimeError("database hiccup")
    worker = KbImportWorker(env.service, env.settings)
    worker.start()
    try:
        doc_id = await upload_one(env)
        worker.wake()
        await wait_for(lambda: env.row(doc_id)["status"] == "PUBLISHED")
        assert worker.running
    finally:
        await worker.stop()


@pytest.mark.asyncio
async def test_worker_heartbeat_and_stop_release_the_document_in_flight(env: Env):
    started, release = threading.Event(), threading.Event()
    inner = env.extractor

    def slow_extract(*args, **kwargs):
        started.set()
        release.wait(10)
        return inner(*args, **kwargs)

    env.service._extract = slow_extract
    worker = KbImportWorker(env.service, env.settings, worker_id="w-stop", heartbeat_seconds=0.05)
    env.service.set_wake_callback(worker.wake)
    worker.start()
    try:
        doc_id = await upload_one(env)
        await wait_for(started.is_set)
        await wait_for(lambda: len(env.repo.touches) >= 2)  # heartbeat while extracting
        assert env.row(doc_id)["extraction_status"] == "RUNNING"
        await worker.stop()
        row = env.row(doc_id)
        assert row["status"] == "FAILED" and row["failed_stage"] == "extraction"
        assert row["error_message"] == INTERRUPTED_REASON
        assert (await env.service.retry(SA, doc_id)).status == "QUEUED"  # retryable
    finally:
        release.set()
        await worker.stop()


@pytest.mark.asyncio
async def test_stale_rows_are_recovered_on_the_next_poll(env: Env):
    old = (utc_now() - timedelta(minutes=30)).isoformat()
    stale = env.repo.seed(status="PROCESSING", extraction_status="RUNNING", updated_at=old, started_at=old, worker_id="dead")
    fresh = env.repo.seed(status="PROCESSING", embedding_status="RUNNING", extraction_status="COMPLETED",
                          chunking_status="COMPLETED", worker_id="alive")
    worker = KbImportWorker(env.service, env.settings)
    worker.start()
    try:
        await wait_for(lambda: env.row(stale)["status"] == "FAILED")
    finally:
        await worker.stop()
    row = env.row(stale)
    assert row["failed_stage"] == "extraction" and row["error_message"] == INTERRUPTED_REASON
    assert row["extraction_status"] == "FAILED" and row["chunking_status"] == "PENDING"
    assert env.row(fresh)["status"] == "PROCESSING"
