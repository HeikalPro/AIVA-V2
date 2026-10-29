"""Download a OneDrive/SharePoint file together with its source metadata."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from crm_ingestion.config import MicrosoftGraphSettings
from crm_ingestion.connectors.sharepoint.auth import ClientCredentialsAuthProvider
from crm_ingestion.connectors.sharepoint.client import SharePointClient
from crm_ingestion.connectors.sharepoint.models import (
    DownloadedFile,
    DriveItem,
    DriveItemReference,
    SourceMetadata,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DocumentDownloader:
    """Turns a DriveItemReference into a DownloadedFile (bytes + SourceMetadata)."""

    def __init__(self, client: SharePointClient, *, clock: Callable[[], datetime] | None = None) -> None:
        self._client = client
        self._clock = clock or _utcnow

    @classmethod
    def from_settings(cls, settings: MicrosoftGraphSettings) -> DocumentDownloader:
        """Wire client-credentials auth and a SharePointClient from settings."""
        return cls(SharePointClient(ClientCredentialsAuthProvider(settings), settings))

    @property
    def client(self) -> SharePointClient:
        return self._client

    def close(self) -> None:
        self._client.close()

    def download(self, reference: DriveItemReference) -> DownloadedFile:
        """Resolve the reference, download its content and attach source metadata."""
        item = self._client.get_item(reference)
        content = self._client.download_content(item)
        metadata = self._metadata(item, content, reference.sharing_url)
        return DownloadedFile(
            content=content,
            filename=item.name,
            mime_type=metadata.mime_type,
            metadata=metadata,
        )

    def download_by_ids(self, drive_id: str, item_id: str) -> DownloadedFile:
        return self.download(DriveItemReference.from_ids(drive_id, item_id))

    def download_sharing_url(self, url: str) -> DownloadedFile:
        return self.download(DriveItemReference.from_sharing_url(url))

    def _metadata(self, item: DriveItem, content: bytes, sharing_url: str | None) -> SourceMetadata:
        mime_type = item.mime_type or mimetypes.guess_type(item.name)[0]
        candidates: dict[str, Any] = {
            "parent_path": item.parent_path,
            "site_id": item.site_id,
            "sha256_hash": item.sha256_hash,
            "quick_xor_hash": item.quick_xor_hash,
            "sharing_url": sharing_url,
        }
        return SourceMetadata(
            source_system="sharepoint" if item.site_id else "onedrive",
            filename=item.name,
            mime_type=mime_type,
            size=len(content),
            source_uri=item.web_url or sharing_url,
            drive_id=item.drive_id,
            item_id=item.id,
            etag=item.etag,
            created_at=item.created_at,
            modified_at=item.modified_at,
            created_by=item.created_by,
            modified_by=item.modified_by,
            retrieved_at=self._clock(),
            extra={k: v for k, v in candidates.items() if v is not None},
        )
