"""Hand-built stand-ins for document-extractor output and the SharePoint downloader."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Sequence
from datetime import UTC, datetime

from document_extractor import Block, Document, ExtractionWarning, Page, Provenance

from crm_ingestion.connectors.sharepoint.models import DownloadedFile, DriveItemReference, SourceMetadata

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FIXED_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return FIXED_NOW


def make_document(
    texts: Sequence[str] = ("Acme Corporation", "Contact: jane@acme.example"),
    *,
    media_type: str = DOCX_MEDIA_TYPE,
    source_name: str | None = None,
    metadata: dict[str, object] | None = None,
    warnings: Sequence[ExtractionWarning] = (),
    content: bytes = b"fake",
) -> Document:
    """A one-page Document shaped like document-extractor output."""
    blocks = [
        Block(
            id=f"b{i}",
            kind="heading" if i == 0 else "paragraph",
            text=text,
            reading_index=i,
            provenance=[Provenance(page=1)],
            level=1 if i == 0 else None,
        )
        for i, text in enumerate(texts)
    ]
    return Document(
        id=hashlib.sha256(content).hexdigest()[:16],
        media_type=media_type,
        source_name=source_name,
        metadata=dict(metadata or {}),
        pages=[Page(number=1, block_ids=[b.id for b in blocks])],
        blocks=blocks,
        warnings=list(warnings),
    )


def make_source_metadata(filename: str = "acme.docx", **overrides: object) -> SourceMetadata:
    data: dict[str, object] = {
        "source_system": "sharepoint",
        "filename": filename,
        "mime_type": DOCX_MEDIA_TYPE,
        "size": 4,
        "source_uri": f"https://contoso.sharepoint.com/sites/crm/Shared%20Documents/{filename}",
        "drive_id": "drive-1",
        "item_id": "item-1",
        "etag": '"{ABC},1"',
        "created_at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        "modified_at": datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC),
        "created_by": "Jane Doe",
        "modified_by": "John Roe",
        "retrieved_at": FIXED_NOW,
        "extra": {"site_id": "site-1"},
    }
    data.update(overrides)
    return SourceMetadata.model_validate(data)


def make_downloaded_file(
    content: bytes = b"fake",
    *,
    filename: str = "acme.docx",
    mime_type: str | None = DOCX_MEDIA_TYPE,
    metadata: SourceMetadata | None = None,
) -> DownloadedFile:
    meta = metadata or make_source_metadata(filename, mime_type=mime_type, size=len(content))
    return DownloadedFile(content=content, filename=filename, mime_type=mime_type, metadata=meta)


class FakeExtractor:
    """Records calls; returns a fresh Document per call or raises a configured error."""

    def __init__(
        self,
        document: Document | None = None,
        *,
        error: Exception | None = None,
        fail_on: set[str] | None = None,
    ) -> None:
        self.document = document
        self.error = error
        self.fail_on = fail_on
        self.calls: list[tuple[bytes, str | None]] = []

    def extract(self, source: bytes, *, filename: str | None = None) -> Document:
        self.calls.append((source, filename))
        if self.error is not None and (self.fail_on is None or filename in self.fail_on):
            raise self.error
        if self.document is not None:
            return self.document
        return make_document(content=source)


class FakeFileSource:
    """A `FileSource` serving prepared files keyed by reference."""

    def __init__(self, files: dict[DriveItemReference, DownloadedFile]) -> None:
        self.files = files
        self.requested: list[DriveItemReference] = []

    def download(self, reference: DriveItemReference) -> DownloadedFile:
        self.requested.append(reference)
        return self.files[reference]


def make_docx_bytes(
    *,
    heading: str = "Acme Corporation",
    paragraph: str = "Contact Jane Doe at jane.doe@acme.example or +1 (555) 010-2030.",
    table: Sequence[Sequence[str]] = (("Name", "Role"), ("Jane Doe", "CEO"), ("John Roe", "CTO")),
) -> bytes:
    """A small DOCX built in memory with python-docx."""
    import docx

    d = docx.Document()
    d.add_heading(heading, level=1)
    d.add_paragraph(paragraph)
    if table:
        t = d.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                t.cell(r, c).text = value
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()
