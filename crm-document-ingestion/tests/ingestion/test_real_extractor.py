"""Integration: the real document-extractor on an in-memory DOCX (fast mode, no OCR, no network)."""

from __future__ import annotations

import pytest

from crm_ingestion.config import ExtractionSettings, Settings
from crm_ingestion.ingestion import DocumentExtractorAdapter, IngestionPipeline
from crm_ingestion.ingestion.pipeline import DOCUMENT_EXTRACTOR_CONFIG_ENV

from .fakes import DOCX_MEDIA_TYPE, fixed_clock, make_docx_bytes, make_downloaded_file

pytest.importorskip("docx")


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> IngestionPipeline:
    # The user's settings file enables an LLM intelligence endpoint; never let it be read here.
    monkeypatch.delenv(DOCUMENT_EXTRACTOR_CONFIG_ENV, raising=False)
    settings = Settings(extraction=ExtractionSettings(mode="fast"))
    return IngestionPipeline(settings=settings, clock=fixed_clock)


def test_real_docx_extraction_preserves_source(pipeline: IngestionPipeline) -> None:
    content = make_docx_bytes()
    file = make_downloaded_file(content, filename="Acme Profile.docx", mime_type=DOCX_MEDIA_TYPE)

    result = pipeline.ingest(file)
    doc = result.document

    assert doc.media_type == DOCX_MEDIA_TYPE
    assert doc.source_name == "Acme Profile.docx"
    assert doc.metadata["source"]["filename"] == "Acme Profile.docx"
    assert doc.metadata["source"]["source_system"] == "sharepoint"
    assert not doc.intelligence  # intelligence stays off without the settings file

    texts = [b.text for b in doc.blocks]
    joined = "\n".join(texts)
    assert any(b.kind == "heading" and "Acme Corporation" in b.text for b in doc.blocks)
    assert "jane.doe@acme.example" in joined
    assert "+1 (555) 010-2030" in joined
    tables = [b.table for b in doc.blocks if b.table is not None]
    assert tables, "expected the DOCX table to be extracted"
    assert ["Jane Doe", "CEO"] in tables[0].to_rows()

    assert not any(w.startswith("mime_type_mismatch") for w in result.warnings)
    assert result.to_dict()["media_type"] == DOCX_MEDIA_TYPE


def test_real_extractor_errors_are_wrapped(pipeline: IngestionPipeline) -> None:
    from document_extractor import ExtractionError

    from crm_ingestion.errors import DocumentExtractionFailed

    file = make_downloaded_file(b"PK\x03\x04 definitely not a real docx", filename="broken.docx")
    with pytest.raises(DocumentExtractionFailed) as info:
        pipeline.ingest(file)
    assert isinstance(info.value.__cause__, (ExtractionError, ValueError, TypeError))


def test_adapter_uses_settings_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DOCUMENT_EXTRACTOR_CONFIG_ENV, raising=False)
    adapter = DocumentExtractorAdapter(ExtractionSettings(mode="fast"))
    adapter.extract(make_docx_bytes(), filename="a.docx")
    built = adapter._extractor
    assert built is not None
    assert built.settings is None
    assert built.default_options.mode == "fast"
