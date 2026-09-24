"""Document ingestion: DownloadedFile -> document-extractor Document + source metadata."""

from __future__ import annotations

from .local import downloaded_file_from_path
from .models import IngestedDocument
from .pipeline import (
    DocumentExtractorAdapter,
    Extractor,
    FileSource,
    IngestionPipeline,
    IngestionResult,
)

__all__ = [
    "DocumentExtractorAdapter",
    "Extractor",
    "FileSource",
    "IngestedDocument",
    "IngestionPipeline",
    "IngestionResult",
    "downloaded_file_from_path",
]
