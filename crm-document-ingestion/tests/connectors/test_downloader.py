from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from crm_ingestion.connectors.sharepoint import (
    ClientCredentialsAuthProvider,
    DocumentDownloader,
    DriveItemReference,
)
from crm_ingestion.errors import ConfigurationError, ItemNotFoundError

from .fakes import (
    OD_DRIVE_ID,
    OD_ITEM_ID,
    SP_DRIVE_ID,
    SP_ITEM_ID,
    SP_SITE_ID,
    graph_error,
    graph_settings,
    make_client,
    onedrive_item,
    sharepoint_item,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_download_sharepoint_by_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(200, json=sharepoint_item())
        return httpx.Response(200, content=b"%PDF-1.7 abc")

    downloader = DocumentDownloader(make_client(handler), clock=lambda: NOW)
    result = downloader.download_by_ids(SP_DRIVE_ID, SP_ITEM_ID)

    assert result.content == b"%PDF-1.7 abc"
    assert result.filename == "Q3 Contract - Fabrikam.pdf"
    assert result.mime_type == "application/pdf"
    meta = result.metadata
    assert meta.source_system == "sharepoint"
    assert meta.size == 12 == result.size
    assert meta.source_uri is not None and meta.source_uri.startswith(
        "https://contoso.sharepoint.com/sites/Sales"
    )
    assert (meta.drive_id, meta.item_id) == (SP_DRIVE_ID, SP_ITEM_ID)
    assert meta.etag == '"{7B0C1E2D-3F4A-4B5C-8D6E-7F8091A2B3C4},3"'
    assert meta.created_by == "Megan Bowen"
    assert meta.modified_by == "Alex Wilber"
    assert meta.modified_at == datetime(2026, 9, 20, 14, 30, 45, tzinfo=UTC)
    assert meta.retrieved_at == NOW
    assert meta.extra == {
        "parent_path": f"/drives/{SP_DRIVE_ID}/root:/Contracts",
        "site_id": SP_SITE_ID,
        "sha256_hash": "ABCDEF0123456789",
        "quick_xor_hash": "dGhpc2lzYXF1aWNreG9yaGFzaA==",
    }


def test_download_onedrive_sharing_url_with_mime_fallback() -> None:
    sharing = "https://1drv.ms/w/s!AqB2c3D4e5F6gHi?e=AbCdEf"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1.0/shares/u!"):
            return httpx.Response(200, json=onedrive_item(webUrl=None))
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"hello")
        raise AssertionError(f"unexpected {request.url}")

    result = DocumentDownloader(make_client(handler), clock=lambda: NOW).download_sharing_url(sharing)

    meta = result.metadata
    assert meta.source_system == "onedrive"
    assert result.mime_type == meta.mime_type
    assert meta.mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert meta.source_uri == sharing  # no webUrl -> falls back to the sharing url
    assert (meta.drive_id, meta.item_id) == (OD_DRIVE_ID, OD_ITEM_ID)
    assert meta.size == 5
    assert meta.extra == {"parent_path": "/drive/root:/Docs", "sharing_url": sharing}


def test_download_propagates_item_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(404, "itemNotFound", "gone")

    with pytest.raises(ItemNotFoundError):
        DocumentDownloader(make_client(handler)).download(DriveItemReference.from_ids("d", "i"))


def test_from_settings_builds_without_credentials_and_fails_at_token_time() -> None:
    downloader = DocumentDownloader.from_settings(graph_settings())
    try:
        assert isinstance(downloader.client._auth, ClientCredentialsAuthProvider)
        with pytest.raises(ConfigurationError, match="MICROSOFT_TENANT_ID"):
            downloader.download_by_ids("d", "i")
    finally:
        downloader.close()
