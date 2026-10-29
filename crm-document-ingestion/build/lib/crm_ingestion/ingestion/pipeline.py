"""file -> connector -> extractor -> Document.

`IngestionPipeline` turns a `DownloadedFile` (bytes + source metadata) into an
`IngestedDocument`: the document-extractor `Document` with its provenance attached.
The extractor is injected through the `Extractor` protocol so tests and future
backends can replace document-extractor without touching the pipeline.

Error policy: `ExtractionError` (every document-extractor failure) plus `ValueError`
and `TypeError` (bad options / unexpected input shapes) raised by the extractor are
wrapped in `DocumentExtractionFailed`, chained with `from`. Anything else is a bug and
propagates unchanged, so it is not silently counted as "a bad file" in a batch.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from document_extractor import Document, ExtractionError

from ..config import ExtractionSettings, Settings, get_settings
from ..connectors.sharepoint.models import DownloadedFile, DriveItemReference
from ..errors import DocumentExtractionFailed, IngestionError
from .models import IngestedDocument

if TYPE_CHECKING:
    from document_extractor import DocumentExtractor

__all__ = [
    "DOCUMENT_EXTRACTOR_CONFIG_ENV",
    "DocumentExtractorAdapter",
    "Extractor",
    "FileSource",
    "IngestionPipeline",
    "IngestionResult",
]

DOCUMENT_EXTRACTOR_CONFIG_ENV = "DOCUMENT_EXTRACTOR_CONFIG"

# Exceptions from the extractor that mean "this file could not be extracted".
_WRAPPED_ERRORS: tuple[type[Exception], ...] = (ExtractionError, ValueError, TypeError)

# Declared types that say nothing about the format; never worth a mismatch warning.
_GENERIC_MIME_TYPES = frozenset(
    {
        "application/octet-stream",
        "binary/octet-stream",
        "application/binary",
        "application/unknown",
        "application/x-unknown",
        "application/download",
        "application/force-download",
        "application/zip",  # OOXML / ODF containers are zips
        "application/x-zip-compressed",
        "text/plain",  # servers use it as a catch-all for text formats
    }
)

# Spellings of the same type, mapped to one canonical form.
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "text/xml": "application/xml",
    "application/x-pdf": "application/pdf",
    "text/x-markdown": "text/markdown",
    "application/csv": "text/csv",
    "text/comma-separated-values": "text/csv",
    "image/x-png": "image/png",
    "image/tif": "image/tiff",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_mime(value: str | None) -> str | None:
    """Lower-case, parameter-free, alias-resolved MIME type; None when unset/empty."""
    if not value:
        return None
    base = value.split(";", 1)[0].strip().lower()
    if not base:
        return None
    return _MIME_ALIASES.get(base, base)


def _mime_mismatch_warning(declared: str | None, detected: str | None) -> str | None:
    """A warning when the declared and detected types meaningfully disagree, else None."""
    d, m = _normalize_mime(declared), _normalize_mime(detected)
    if d is None or m is None or d == m:
        return None
    if d in _GENERIC_MIME_TYPES or m in _GENERIC_MIME_TYPES:
        return None
    return f"mime_type_mismatch: declared {declared!r} but document-extractor detected {detected!r}"


@runtime_checkable
class Extractor(Protocol):
    """Anything that turns file bytes into a document-extractor `Document`."""

    def extract(self, source: bytes, *, filename: str | None = None) -> Document:
        """Extract `source`; `filename` is a type-detection hint."""
        ...


@runtime_checkable
class FileSource(Protocol):
    """Anything that can fetch a drive item (e.g. the SharePoint `DocumentDownloader`)."""

    def download(self, reference: DriveItemReference) -> DownloadedFile:
        """Download the referenced item's bytes and metadata."""
        ...


class DocumentExtractorAdapter:
    """`Extractor` backed by `document_extractor.DocumentExtractor`.

    When DOCUMENT_EXTRACTOR_CONFIG is set the library reads that settings file itself;
    otherwise `ExtractionOptions(mode=settings.mode)` is used. The underlying extractor
    is built on the first `extract` call, so constructing the adapter is cheap."""

    def __init__(self, settings: ExtractionSettings | None = None) -> None:
        self._settings = settings if settings is not None else ExtractionSettings()
        self._extractor: DocumentExtractor | None = None
        self._lock = threading.Lock()

    @property
    def settings(self) -> ExtractionSettings:
        """The extraction settings this adapter was built from."""
        return self._settings

    def _build(self) -> DocumentExtractor:
        from document_extractor import DocumentExtractor, ExtractionOptions

        if os.environ.get(DOCUMENT_EXTRACTOR_CONFIG_ENV, "").strip():
            return DocumentExtractor()  # library reads the settings file named by the env var
        return DocumentExtractor(ExtractionOptions(mode=self._settings.mode))

    def _get(self) -> DocumentExtractor:
        if self._extractor is None:
            with self._lock:
                if self._extractor is None:
                    self._extractor = self._build()
        return self._extractor

    def extract(self, source: bytes, *, filename: str | None = None) -> Document:
        """Extract `source` with the lazily built document-extractor."""
        result: Document = self._get().extract(source, filename=filename)
        return result


@dataclass(frozen=True, slots=True)
class IngestionResult:
    """Outcome of one file in `IngestionPipeline.ingest_many`: exactly one of
    `ingested` / `error` is set."""

    filename: str
    ingested: IngestedDocument | None = None
    error: IngestionError | None = None

    @property
    def ok(self) -> bool:
        """True when the file was ingested."""
        return self.ingested is not None


class IngestionPipeline:
    """Runs downloaded files through an `Extractor` and attaches source metadata."""

    def __init__(
        self,
        extractor: Extractor | None = None,
        *,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if extractor is None:
            extraction = settings.extraction if settings is not None else get_settings().extraction
            extractor = DocumentExtractorAdapter(extraction)
        self._extractor: Extractor = extractor
        self._clock: Callable[[], datetime] = clock if clock is not None else _utc_now

    @property
    def extractor(self) -> Extractor:
        """The extractor this pipeline calls."""
        return self._extractor

    def ingest(self, file: DownloadedFile) -> IngestedDocument:
        """Extract one file. Raises `DocumentExtractionFailed` for empty content or an
        extractor failure (the original exception is `__cause__`)."""
        if not file.content:
            raise DocumentExtractionFailed(file.filename, ValueError("file content is empty"))
        try:
            document = self._extractor.extract(file.content, filename=file.filename)
        except _WRAPPED_ERRORS as exc:
            raise DocumentExtractionFailed(file.filename, exc) from exc

        document.source_name = file.filename
        document.metadata["source"] = file.metadata.model_dump(mode="json")

        warnings = [f"{w.code}: {w.message}" for w in document.warnings]
        mismatch = _mime_mismatch_warning(file.mime_type or file.metadata.mime_type, document.media_type)
        if mismatch is not None:
            warnings.append(mismatch)

        return IngestedDocument(
            document=document,
            source=file.metadata,
            ingested_at=self._clock(),
            content_sha256=hashlib.sha256(file.content).hexdigest(),
            warnings=warnings,
        )

    def ingest_from(self, source: FileSource, reference: DriveItemReference) -> IngestedDocument:
        """Download `reference` from `source`, then `ingest` it. Download errors propagate."""
        return self.ingest(source.download(reference))

    def ingest_many(self, files: Iterable[DownloadedFile]) -> Iterator[IngestionResult]:
        """Ingest each file lazily; an `IngestionError` on one file is reported in its
        result and the batch continues. Other exceptions (bugs) propagate."""
        for file in files:
            try:
                yield IngestionResult(filename=file.filename, ingested=self.ingest(file))
            except IngestionError as exc:
                yield IngestionResult(filename=file.filename, error=exc)
