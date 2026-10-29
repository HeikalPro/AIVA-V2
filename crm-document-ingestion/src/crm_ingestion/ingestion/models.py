"""The pipeline's output: a document-extractor Document plus its source metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from document_extractor import Document

from ..connectors.sharepoint.models import SourceMetadata


@dataclass(slots=True)
class IngestedDocument:
    """What the pipeline returns and the CRM layer consumes.

    `document` is exactly what document-extractor produced (its `source_name` set to
    the original filename and its `metadata["source"]` holding `source.model_dump()`),
    so code that only sees the Document still knows where it came from."""

    document: Document
    source: SourceMetadata
    ingested_at: datetime
    content_sha256: str
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, *, include_document: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "source": self.source.model_dump(mode="json"),
            "ingested_at": self.ingested_at.isoformat(),
            "content_sha256": self.content_sha256,
            "warnings": list(self.warnings),
            "document_id": self.document.id,
            "media_type": self.document.media_type,
        }
        if include_document:
            out["document"] = self.document.to_dict(include_image_data=False)
        return out
