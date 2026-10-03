"""Hand-built document-extractor output for the CRM tests (no files, no OCR, no network)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from document_extractor import Block, Document, Provenance, Table, TableCell

from crm_ingestion.connectors.sharepoint.models import SourceMetadata
from crm_ingestion.ingestion.models import IngestedDocument

FIXED_NOW = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


def text_block(
    block_id: str,
    text: str,
    *,
    page: int = 1,
    index: int = 0,
    kind: str = "paragraph",
    bbox: tuple[float, float, float, float] | None = (10.0, 20.0, 300.0, 40.0),
    confidence: float | None = None,
) -> Block:
    """A text block with one provenance entry."""
    return Block(
        id=block_id,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        reading_index=index,
        provenance=[Provenance(page=page, bbox=bbox, confidence=confidence)],
    )


def table_block(
    block_id: str, rows: list[list[str]], *, page: int = 1, index: int = 0, confidence: float | None = None
) -> Block:
    """A table block built from a grid of cell texts."""
    cells = [TableCell(row=r, col=c, text=t) for r, row in enumerate(rows) for c, t in enumerate(row)]
    n_cols = max((len(r) for r in rows), default=0)
    table = Table(cells=cells, n_rows=len(rows), n_cols=n_cols, confidence=confidence)
    return Block(
        id=block_id,
        kind="table",
        text="\n".join("\t".join(r) for r in rows),
        reading_index=index,
        provenance=[Provenance(page=page, bbox=(0.0, 0.0, 500.0, 200.0))],
        table=table,
    )


def make_document(
    blocks: list[Block],
    *,
    doc_id: str = "doc0001",
    intelligence: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Document:
    """A Document with blocks in the given reading order."""
    for i, b in enumerate(blocks):
        b.reading_index = i
    return Document(
        id=doc_id,
        media_type="application/pdf",
        source_name="sample.pdf",
        metadata=dict(metadata or {}),
        blocks=blocks,
        intelligence=dict(intelligence or {}),
    )


def make_source(filename: str = "sample.pdf") -> SourceMetadata:
    return SourceMetadata(
        source_system="sharepoint",
        filename=filename,
        mime_type="application/pdf",
        size=1234,
        source_uri="https://contoso.sharepoint.com/sites/x/sample.pdf",
        drive_id="drv1",
        item_id="itm1",
        retrieved_at=FIXED_NOW,
    )


def make_ingested(document: Document, *, warnings: list[str] | None = None) -> IngestedDocument:
    source = make_source(document.source_name or "sample.pdf")
    document.metadata["source"] = source.model_dump(mode="json")
    return IngestedDocument(
        document=document,
        source=source,
        ingested_at=FIXED_NOW,
        content_sha256="0" * 64,
        warnings=list(warnings or []),
    )


def business_letter() -> Document:
    """A generic letter: company block, contact lines, a footer, and a key/value table."""
    return make_document(
        [
            text_block("b0", "ACME Trading LLC", kind="heading"),
            text_block(
                "b1",
                "Company: ACME Trading LLC\nTax ID: 123-456-789\nWebsite: acme-trading.example.com",
                index=1,
            ),
            text_block(
                "b2",
                "Contact Person: Sara Ahmed\nJob Title: Procurement Manager\n"
                "Email: sara.ahmed@acme-trading.example.com\nMobile: +20 100 123 4567",
                index=2,
            ),
            table_block(
                "b3",
                [
                    ["Reference No.", "REF-2024-0042"],
                    ["Date", "01/03/2024"],
                    ["Total Amount", "12,500.00"],
                    ["Currency", "EGP"],
                ],
                index=3,
            ),
            text_block(
                "b4",
                "For enquiries write to info@acme-trading.example.com or visit https://acme-trading.example.com.",
                page=2,
                kind="footer",
                index=4,
            ),
        ]
    )


INTELLIGENCE: dict[str, Any] = {
    "version": 1,
    "document_type": "invoice",
    "confidence": {"value": 0.92, "source": "model-reported", "verified": False},
    "entities": [
        {
            "type": "organization",
            "text": "ACME Trading LLC",
            "confidence": {"value": 0.95, "source": "model-reported", "verified": False},
            "provenance": [
                {"block_id": "b0", "page": 1, "bbox": [10, 20, 300, 40], "source_text": "ACME Trading LLC"}
            ],
        },
        {
            "type": "person",
            "text": "Sara Ahmed",
            "confidence": {"value": 0.7},
            "provenance": [
                {"block_id": "b2", "page": 1, "bbox": None, "source_text": "Contact Person: Sara Ahmed"}
            ],
        },
        {"type": "location", "text": "Cairo", "confidence": {"value": 0.8}, "provenance": []},
    ],
    "extracted_fields": {
        "invoice_number": {
            "value": "REF-2024-0042",
            "confidence": {"value": 0.97},
            "provenance": [{"block_id": "b3", "page": 1, "bbox": None, "source_text": "REF-2024-0042"}],
        },
        "tax_id": {"value": "123-456-789", "confidence": {"value": 0.6}, "provenance": []},
        "payment_terms": {"value": "Net 30", "confidence": {"value": 0.8}, "provenance": []},
    },
    "dates": [
        {
            "value": "2024-03-01",
            "text": "01/03/2024",
            "role": "issue_date",
            "confidence": {"value": 0.9},
            "provenance": [{"block_id": "b3", "page": 1}],
        }
    ],
    "summary": "An invoice from ACME Trading LLC.",
}


# A test-only schema declared purely as data: proves client fields need no code.
CUSTOM_SCHEMA = {
    "name": "service_request",
    "description": "A test-only schema declared purely as data.",
    "aliases": ["ticket"],
    "fields": [
        {
            "name": "request_id",
            "type": "string",
            "required": True,
            "aliases": ["Request #", "Ticket No"],
            "pattern": r"SR-\d{4}",
        },
        {"name": "opened_on", "type": "date", "aliases": ["Opened"]},
        {"name": "units", "type": "integer", "aliases": ["Qty"]},
        {"name": "tags", "type": "list"},
    ],
}
