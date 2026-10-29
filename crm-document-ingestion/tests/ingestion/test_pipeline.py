"""IngestionPipeline against a fake extractor (no document-extractor parsing, no network)."""

from __future__ import annotations

import hashlib

import pytest
from document_extractor import DocumentParsingError, ExtractionWarning, UnsupportedFormatError

from crm_ingestion.config import ExtractionSettings, Settings
from crm_ingestion.connectors.sharepoint.models import DriveItemReference
from crm_ingestion.errors import DocumentExtractionFailed, IngestionError
from crm_ingestion.ingestion import (
    DocumentExtractorAdapter,
    Extractor,
    FileSource,
    IngestedDocument,
    IngestionPipeline,
    IngestionResult,
)

from .fakes import (
    DOCX_MEDIA_TYPE,
    FIXED_NOW,
    FakeExtractor,
    FakeFileSource,
    fixed_clock,
    make_document,
    make_downloaded_file,
)


def _pipeline(extractor: FakeExtractor) -> IngestionPipeline:
    return IngestionPipeline(extractor, clock=fixed_clock)


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeExtractor(), Extractor)
    assert isinstance(FakeFileSource({}), FileSource)
    assert isinstance(DocumentExtractorAdapter(ExtractionSettings(mode="fast")), Extractor)


def test_ingest_calls_extractor_with_content_and_filename() -> None:
    extractor = FakeExtractor()
    file = make_downloaded_file(b"PK-bytes", filename="Contract.docx")
    _pipeline(extractor).ingest(file)
    assert extractor.calls == [(b"PK-bytes", "Contract.docx")]


def test_ingest_preserves_source_metadata() -> None:
    doc = make_document(source_name="tmpXYZ.bin", metadata={"title": "Acme deck", "author": "Jane"})
    file = make_downloaded_file(b"data", filename="acme.docx")

    result = _pipeline(FakeExtractor(doc)).ingest(file)

    assert isinstance(result, IngestedDocument)
    assert result.document is doc
    assert result.source == file.metadata
    assert result.ingested_at == FIXED_NOW
    assert doc.source_name == "acme.docx"
    # library metadata keys survive; source is added alongside them
    assert doc.metadata["title"] == "Acme deck"
    assert doc.metadata["author"] == "Jane"
    source = doc.metadata["source"]
    assert source == file.metadata.model_dump(mode="json")
    assert source["filename"] == "acme.docx"
    assert source["source_system"] == "sharepoint"
    assert source["drive_id"] == "drive-1"
    assert source["modified_at"] == "2026-02-03T04:05:06Z"
    assert source["extra"] == {"site_id": "site-1"}


def test_ingest_document_round_trips_with_source() -> None:
    doc = make_document()
    result = _pipeline(FakeExtractor(doc)).ingest(make_downloaded_file())
    as_dict = result.to_dict()
    assert as_dict["document"]["metadata"]["source"]["filename"] == "acme.docx"
    assert as_dict["content_sha256"] == result.content_sha256


def test_ingest_computes_content_sha256() -> None:
    content = b"some document bytes"
    result = _pipeline(FakeExtractor()).ingest(make_downloaded_file(content))
    assert result.content_sha256 == hashlib.sha256(content).hexdigest()


def test_ingest_maps_extractor_warnings() -> None:
    doc = make_document(
        warnings=[
            ExtractionWarning(code="ocr_low_confidence", message="page 2 is blurry", page=2),
            ExtractionWarning(code="table_split", message="table spans pages"),
        ]
    )
    result = _pipeline(FakeExtractor(doc)).ingest(make_downloaded_file())
    assert result.warnings == ["ocr_low_confidence: page 2 is blurry", "table_split: table spans pages"]


def test_ingest_warns_on_meaningful_mime_mismatch() -> None:
    doc = make_document(media_type="application/pdf")
    result = _pipeline(FakeExtractor(doc)).ingest(make_downloaded_file(mime_type=DOCX_MEDIA_TYPE))
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("mime_type_mismatch:")
    assert "application/pdf" in result.warnings[0]


@pytest.mark.parametrize(
    ("declared", "detected"),
    [
        (None, "application/pdf"),
        ("application/octet-stream", "application/pdf"),
        ("application/zip", DOCX_MEDIA_TYPE),
        ("application/pdf", "application/octet-stream"),
        ("APPLICATION/PDF; charset=binary", "application/pdf"),
        ("image/jpg", "image/jpeg"),
        (DOCX_MEDIA_TYPE, DOCX_MEDIA_TYPE),
    ],
)
def test_ingest_is_lenient_about_mime(declared: str | None, detected: str) -> None:
    doc = make_document(media_type=detected)
    file = make_downloaded_file(mime_type=declared)
    result = _pipeline(FakeExtractor(doc)).ingest(file)
    assert result.warnings == []


def test_ingest_falls_back_to_metadata_mime_type() -> None:
    from .fakes import make_source_metadata

    meta = make_source_metadata(mime_type="application/pdf")
    file = make_downloaded_file(mime_type=None, metadata=meta)
    result = _pipeline(FakeExtractor(make_document(media_type=DOCX_MEDIA_TYPE))).ingest(file)
    assert any(w.startswith("mime_type_mismatch:") for w in result.warnings)


@pytest.mark.parametrize(
    "error",
    [
        DocumentParsingError("corrupt zip"),
        UnsupportedFormatError("application/x-weird"),
        ValueError("bad option"),
        TypeError("bad input"),
    ],
)
def test_ingest_wraps_extractor_errors(error: Exception) -> None:
    file = make_downloaded_file(filename="broken.docx")
    with pytest.raises(DocumentExtractionFailed) as info:
        _pipeline(FakeExtractor(error=error)).ingest(file)
    assert info.value.filename == "broken.docx"
    assert info.value.__cause__ is error
    assert "broken.docx" in str(info.value)


def test_ingest_does_not_wrap_unexpected_errors() -> None:
    with pytest.raises(RuntimeError):
        _pipeline(FakeExtractor(error=RuntimeError("bug"))).ingest(make_downloaded_file())


def test_ingest_rejects_empty_content_without_calling_extractor() -> None:
    extractor = FakeExtractor()
    with pytest.raises(DocumentExtractionFailed, match="empty"):
        _pipeline(extractor).ingest(make_downloaded_file(b"", filename="empty.pdf"))
    assert extractor.calls == []


def test_ingest_from_downloads_then_ingests() -> None:
    ref = DriveItemReference.from_ids("drive-1", "item-1")
    file = make_downloaded_file(b"remote bytes", filename="remote.docx")
    source = FakeFileSource({ref: file})

    result = _pipeline(FakeExtractor()).ingest_from(source, ref)

    assert source.requested == [ref]
    assert result.source.filename == "remote.docx"
    assert result.document.metadata["source"]["item_id"] == "item-1"
    assert result.content_sha256 == hashlib.sha256(b"remote bytes").hexdigest()


def test_ingest_many_continues_after_an_error() -> None:
    files = [
        make_downloaded_file(b"one", filename="one.docx"),
        make_downloaded_file(b"two", filename="two.docx"),
        make_downloaded_file(b"", filename="empty.docx"),
        make_downloaded_file(b"three", filename="three.docx"),
    ]
    extractor = FakeExtractor(error=DocumentParsingError("corrupt"), fail_on={"two.docx"})

    results = list(_pipeline(extractor).ingest_many(files))

    assert [r.filename for r in results] == ["one.docx", "two.docx", "empty.docx", "three.docx"]
    assert [r.ok for r in results] == [True, False, False, True]
    assert all(isinstance(r, IngestionResult) for r in results)
    failed = results[1]
    assert failed.ingested is None
    assert isinstance(failed.error, DocumentExtractionFailed)
    assert isinstance(failed.error, IngestionError)
    assert isinstance(failed.error.__cause__, DocumentParsingError)
    assert results[3].ingested is not None
    assert results[3].ingested.source.filename == "three.docx"


def test_ingest_many_is_lazy() -> None:
    extractor = FakeExtractor()
    it = _pipeline(extractor).ingest_many(make_downloaded_file(filename=f"{i}.docx") for i in range(3))
    assert extractor.calls == []
    next(it)
    assert len(extractor.calls) == 1


def test_default_extractor_is_adapter_built_lazily() -> None:
    pipeline = IngestionPipeline(settings=Settings(extraction=ExtractionSettings(mode="fast")))
    adapter = pipeline.extractor
    assert isinstance(adapter, DocumentExtractorAdapter)
    assert adapter.settings.mode == "fast"
    assert adapter._extractor is None  # nothing built until the first extract call
