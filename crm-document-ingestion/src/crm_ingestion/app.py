"""One object for the whole chain: file or drive item -> Document -> CRM entities.

`CRMIngestionApp` composes the SharePoint `DocumentDownloader`, the `IngestionPipeline`
and the `CRMExtractionService`. The downloader is built only when a SharePoint/OneDrive
reference is processed, so local files need no Microsoft credentials."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from types import TracebackType
from typing import Self

from .config import Settings, get_settings
from .connectors.sharepoint.downloader import DocumentDownloader
from .connectors.sharepoint.models import DownloadedFile, DriveItemReference
from .crm.service import CRMExtractionService, CRMProcessingResult
from .ingestion.local import downloaded_file_from_path
from .ingestion.models import IngestedDocument
from .ingestion.pipeline import FileSource, IngestionPipeline

__all__ = ["CRMIngestionApp"]


class CRMIngestionApp:
    """Façade over downloader -> pipeline -> CRM service.

    Every component can be injected (tests, custom extractors); missing ones are built
    from `settings` (default: `get_settings()`). `downloader_factory` replaces the
    default `DocumentDownloader.from_settings(settings.microsoft)`."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        pipeline: IngestionPipeline | None = None,
        service: CRMExtractionService | None = None,
        downloader: FileSource | None = None,
        downloader_factory: Callable[[], FileSource] | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        self._pipeline = pipeline if pipeline is not None else IngestionPipeline(settings=self._settings)
        self._service = service if service is not None else CRMExtractionService(settings=self._settings.crm)
        self._downloader = downloader
        self._downloader_factory = downloader_factory
        self._lock = threading.Lock()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def pipeline(self) -> IngestionPipeline:
        return self._pipeline

    @property
    def service(self) -> CRMExtractionService:
        return self._service

    @property
    def downloader(self) -> FileSource:
        """The file source for drive items, built on first use."""
        if self._downloader is None:
            with self._lock:
                if self._downloader is None:
                    factory = self._downloader_factory
                    self._downloader = (
                        factory()
                        if factory is not None
                        else DocumentDownloader.from_settings(self._settings.microsoft)
                    )
        return self._downloader

    # ---- ingestion only (Document + source metadata)

    def ingest_file(self, path: str | os.PathLike[str]) -> IngestedDocument:
        """Extract a local file."""
        return self._pipeline.ingest(downloaded_file_from_path(path))

    def ingest_reference(self, reference: DriveItemReference) -> IngestedDocument:
        """Download a drive item and extract it."""
        return self._pipeline.ingest_from(self.downloader, reference)

    # ---- full chain (CRM entities)

    def process_downloaded(self, file: DownloadedFile) -> CRMProcessingResult:
        """Extract and map an already downloaded file."""
        return self._service.process(self._pipeline.ingest(file))

    def process_file(self, path: str | os.PathLike[str]) -> CRMProcessingResult:
        """Local file -> CRM entities."""
        return self._service.process(self.ingest_file(path))

    def process_reference(self, reference: DriveItemReference) -> CRMProcessingResult:
        """OneDrive/SharePoint item -> CRM entities."""
        return self._service.process(self.ingest_reference(reference))

    def process_sharing_url(self, url: str) -> CRMProcessingResult:
        """Sharing link -> CRM entities."""
        return self.process_reference(DriveItemReference.from_sharing_url(url))

    def process_drive_item(self, drive_id: str, item_id: str) -> CRMProcessingResult:
        """Drive/item ids -> CRM entities."""
        return self.process_reference(DriveItemReference.from_ids(drive_id, item_id))

    # ---- lifecycle

    def close(self) -> None:
        """Close the downloader's HTTP client if one was built here or injected."""
        close = getattr(self._downloader, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
