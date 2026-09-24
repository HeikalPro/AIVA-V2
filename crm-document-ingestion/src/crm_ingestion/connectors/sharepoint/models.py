"""Data shapes shared by the Microsoft Graph connector and the ingestion pipeline.

These are the contract between layers: the connector produces a DownloadedFile, the
pipeline consumes one. Nothing here talks to the network."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DriveItemReference(BaseModel):
    """Where a file lives. Either a sharing URL, or a drive_id + item_id pair."""

    model_config = ConfigDict(frozen=True)

    sharing_url: str | None = None
    drive_id: str | None = None
    item_id: str | None = None

    @model_validator(mode="after")
    def _one_addressing_mode(self) -> Self:
        by_ids = self.drive_id is not None or self.item_id is not None
        if self.sharing_url and by_ids:
            raise ValueError("give either sharing_url, or drive_id and item_id, not both")
        if not self.sharing_url and not (self.drive_id and self.item_id):
            raise ValueError("drive_id and item_id are both required when sharing_url is not given")
        return self

    @classmethod
    def from_sharing_url(cls, url: str) -> DriveItemReference:
        return cls(sharing_url=url)

    @classmethod
    def from_ids(cls, drive_id: str, item_id: str) -> DriveItemReference:
        return cls(drive_id=drive_id, item_id=item_id)


class DriveItem(BaseModel):
    """The subset of a Graph driveItem this project uses.
    https://learn.microsoft.com/graph/api/resources/driveitem"""

    model_config = ConfigDict(frozen=True)

    id: str
    drive_id: str
    name: str
    size: int | None = None
    mime_type: str | None = None
    web_url: str | None = None
    etag: str | None = None
    ctag: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    created_by: str | None = None
    modified_by: str | None = None
    parent_path: str | None = None
    site_id: str | None = None
    is_file: bool = True
    sha256_hash: str | None = None
    quick_xor_hash: str | None = None
    download_url: str | None = None  # pre-authenticated, short-lived (@microsoft.graph.downloadUrl)
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)  # the Graph JSON, for anything not mapped


class SourceMetadata(BaseModel):
    """Where a document came from. Travels with the extracted Document into the CRM layer."""

    model_config = ConfigDict(frozen=True)

    source_system: str  # "sharepoint", "onedrive", "local", ...
    filename: str
    mime_type: str | None = None
    size: int | None = None
    source_uri: str | None = None  # web URL, sharing URL or file path
    drive_id: str | None = None
    item_id: str | None = None
    etag: str | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    created_by: str | None = None
    modified_by: str | None = None
    retrieved_at: datetime | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class DownloadedFile(BaseModel):
    """A file's bytes plus everything known about where it came from."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    content: bytes = Field(repr=False)
    filename: str
    mime_type: str | None = None
    metadata: SourceMetadata

    @property
    def size(self) -> int:
        return len(self.content)
