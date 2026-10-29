from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from crm_ingestion.connectors.sharepoint import DriveItemReference, drive_item_from_graph, encode_sharing_url
from crm_ingestion.errors import GraphAPIError

from .fakes import OD_DRIVE_ID, SP_DRIVE_ID, SP_ITEM_ID, SP_SITE_ID, onedrive_item, sharepoint_item


def test_encode_sharing_url_known_value() -> None:
    # base64("https://1drv.ms/a") == "aHR0cHM6Ly8xZHJ2Lm1zL2E=" -> padding stripped
    assert encode_sharing_url("https://1drv.ms/a") == "u!aHR0cHM6Ly8xZHJ2Lm1zL2E"


def test_encode_sharing_url_uses_url_safe_alphabet_without_padding() -> None:
    # Bytes chosen so standard base64 would contain '+' and '/'.
    url = "https://contoso.sharepoint.com/:w:/s/Sales/E?e=~~~>>>???"
    encoded = encode_sharing_url(url)
    assert encoded.startswith("u!")
    body = encoded[2:]
    assert "+" not in body and "/" not in body and "=" not in body
    assert "+" in base64.b64encode(url.encode()).decode() or "/" in base64.b64encode(url.encode()).decode()
    padded = body + "=" * (-len(body) % 4)
    assert base64.urlsafe_b64decode(padded).decode() == url


def test_encode_sharing_url_rejects_empty() -> None:
    with pytest.raises(ValueError):
        encode_sharing_url("  ")


def test_reference_validation() -> None:
    assert DriveItemReference.from_ids("d", "i").item_id == "i"
    assert DriveItemReference.from_sharing_url("https://x").sharing_url == "https://x"
    with pytest.raises(ValidationError):
        DriveItemReference(drive_id="d")
    with pytest.raises(ValidationError):
        DriveItemReference()
    with pytest.raises(ValidationError):
        DriveItemReference(sharing_url="https://x", drive_id="d", item_id="i")


def test_drive_item_from_graph_sharepoint() -> None:
    item = drive_item_from_graph(sharepoint_item())
    assert item.id == SP_ITEM_ID
    assert item.drive_id == SP_DRIVE_ID
    assert item.site_id == SP_SITE_ID
    assert item.mime_type == "application/pdf"
    assert item.created_by == "Megan Bowen"
    assert item.modified_by == "Alex Wilber"
    assert item.modified_at is not None and item.modified_at.utcoffset() is not None
    assert item.modified_at.isoformat() == "2026-09-20T14:30:45+00:00"
    assert item.parent_path is not None and item.parent_path.endswith("/Contracts")
    assert item.sha256_hash == "ABCDEF0123456789"
    assert item.quick_xor_hash == "dGhpc2lzYXF1aWNreG9yaGFzaA=="
    assert item.download_url is not None
    assert item.is_file
    assert item.raw["name"] == item.name


def test_drive_item_from_graph_onedrive() -> None:
    item = drive_item_from_graph(onedrive_item())
    assert item.drive_id == OD_DRIVE_ID
    assert item.site_id is None
    assert item.mime_type is None
    assert item.created_by == "Adele Vance"  # user preferred over application
    assert item.download_url is None


def test_drive_item_from_graph_requires_drive_id() -> None:
    with pytest.raises(GraphAPIError):
        drive_item_from_graph({"id": "x", "name": "y"})
