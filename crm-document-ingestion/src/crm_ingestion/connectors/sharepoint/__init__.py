"""Microsoft Graph connector for OneDrive and SharePoint files."""

from __future__ import annotations

from crm_ingestion.connectors.sharepoint.auth import (
    AuthenticationProvider,
    ClientCredentialsAuthProvider,
    StaticTokenAuthProvider,
)
from crm_ingestion.connectors.sharepoint.client import (
    SharePointClient,
    drive_from_graph,
    drive_item_from_graph,
    site_from_graph,
)
from crm_ingestion.connectors.sharepoint.downloader import DocumentDownloader
from crm_ingestion.connectors.sharepoint.models import (
    DownloadedFile,
    Drive,
    DriveItem,
    DriveItemReference,
    Site,
    SourceMetadata,
)
from crm_ingestion.connectors.sharepoint.sharing import encode_sharing_url

__all__ = [
    "AuthenticationProvider",
    "ClientCredentialsAuthProvider",
    "DocumentDownloader",
    "DownloadedFile",
    "Drive",
    "DriveItem",
    "DriveItemReference",
    "SharePointClient",
    "Site",
    "SourceMetadata",
    "StaticTokenAuthProvider",
    "drive_from_graph",
    "drive_item_from_graph",
    "encode_sharing_url",
    "site_from_graph",
]
