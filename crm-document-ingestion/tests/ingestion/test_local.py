"""downloaded_file_from_path on real files in tmp_path."""

from __future__ import annotations

from pathlib import Path

from crm_ingestion.ingestion import IngestionPipeline, downloaded_file_from_path

from .fakes import DOCX_MEDIA_TYPE, FIXED_NOW, FakeExtractor, fixed_clock


def test_downloaded_file_from_path(tmp_path: Path) -> None:
    path = tmp_path / "Acme Profile.docx"
    path.write_bytes(b"PK\x03\x04docx-ish")

    file = downloaded_file_from_path(path, clock=fixed_clock)

    assert file.content == b"PK\x03\x04docx-ish"
    assert file.filename == "Acme Profile.docx"
    assert file.mime_type == DOCX_MEDIA_TYPE
    meta = file.metadata
    assert meta.source_system == "local"
    assert meta.filename == "Acme Profile.docx"
    assert meta.mime_type == DOCX_MEDIA_TYPE
    assert meta.size == len(file.content) == file.size
    assert meta.source_uri == path.resolve().as_uri()
    assert meta.source_uri is not None and meta.source_uri.startswith("file:")
    assert meta.retrieved_at == FIXED_NOW
    assert meta.modified_at is not None and meta.modified_at.tzinfo is not None
    assert abs(meta.modified_at.timestamp() - path.stat().st_mtime) < 1e-3


def test_downloaded_file_from_path_accepts_str_and_mime_override(tmp_path: Path) -> None:
    path = tmp_path / "notes.unknownext"
    path.write_text("hello", encoding="utf-8")

    guessed = downloaded_file_from_path(str(path))
    assert guessed.mime_type is None
    assert guessed.metadata.retrieved_at is not None

    forced = downloaded_file_from_path(path, mime_type="text/plain")
    assert forced.mime_type == forced.metadata.mime_type == "text/plain"


def test_local_file_flows_through_pipeline(tmp_path: Path) -> None:
    path = tmp_path / "a.pdf"
    path.write_bytes(b"%PDF-1.7 fake")
    result = IngestionPipeline(FakeExtractor(), clock=fixed_clock).ingest(downloaded_file_from_path(path))
    assert result.document.metadata["source"]["source_system"] == "local"
    assert result.document.metadata["source"]["source_uri"] == path.resolve().as_uri()
