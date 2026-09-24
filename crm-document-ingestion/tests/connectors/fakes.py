"""Fake Microsoft Graph payloads and helpers for connector tests. No network."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx

from crm_ingestion.config import MicrosoftGraphSettings
from crm_ingestion.connectors.sharepoint import SharePointClient, StaticTokenAuthProvider

GRAPH = "https://graph.microsoft.com/v1.0"
SP_DRIVE_ID = "b!9rY3dQ2hAkSgT1k0bXl0ZW5hbnQtc2l0ZS1kcml2ZQ"
SP_ITEM_ID = "01ABCDEFSP7XKQ2ZV5MNBZ3JQ4WGH6YTRE"
SP_SITE_ID = (
    "contoso.sharepoint.com,2c5a1b62-8f0e-4b3b-9f6f-2d0a3e8e1c11,7f1d0a2c-3b4e-4c5d-9e6f-0a1b2c3d4e5f"
)
OD_DRIVE_ID = "a1b2c3d4e5f60718"
OD_ITEM_ID = "A1B2C3D4E5F60718!1234"
SECRET = "super-secret-value-123"

Handler = Callable[[httpx.Request], httpx.Response]


def graph_settings(**overrides: Any) -> MicrosoftGraphSettings:
    """Settings isolated from the real environment and .env (unconfigured by default)."""
    values: dict[str, Any] = {"tenant_id": "", "client_id": "", "client_secret": ""}
    values.update(overrides)
    return MicrosoftGraphSettings(_env_file=None, **values)


def sharepoint_item(**overrides: Any) -> dict[str, Any]:
    """A SharePoint document-library driveItem (has parentReference.siteId)."""
    data: dict[str, Any] = {
        "@odata.context": f"{GRAPH}/$metadata#drives('{SP_DRIVE_ID}')/items/$entity",
        "@microsoft.graph.downloadUrl": "https://contoso.sharepoint.com/sites/Sales/_layouts/15/download.aspx?UniqueId=abc&tempauth=xyz",
        "id": SP_ITEM_ID,
        "name": "Q3 Contract - Fabrikam.pdf",
        "size": 12,
        "webUrl": "https://contoso.sharepoint.com/sites/Sales/Shared%20Documents/Q3%20Contract%20-%20Fabrikam.pdf",
        "eTag": '"{7B0C1E2D-3F4A-4B5C-8D6E-7F8091A2B3C4},3"',
        "cTag": '"c:{7B0C1E2D-3F4A-4B5C-8D6E-7F8091A2B3C4},5"',
        "createdDateTime": "2026-08-01T09:15:00Z",
        "lastModifiedDateTime": "2026-09-20T14:30:45Z",
        "createdBy": {"user": {"email": "megan@contoso.com", "displayName": "Megan Bowen"}},
        "lastModifiedBy": {"user": {"email": "alex@contoso.com", "displayName": "Alex Wilber"}},
        "parentReference": {
            "driveType": "documentLibrary",
            "driveId": SP_DRIVE_ID,
            "id": "01ABCDEFPARENTFOLDERID",
            "path": f"/drives/{SP_DRIVE_ID}/root:/Contracts",
            "siteId": SP_SITE_ID,
        },
        "file": {
            "mimeType": "application/pdf",
            "hashes": {"quickXorHash": "dGhpc2lzYXF1aWNreG9yaGFzaA==", "sha256Hash": "ABCDEF0123456789"},
        },
    }
    data.update(overrides)
    return data


def onedrive_item(**overrides: Any) -> dict[str, Any]:
    """A OneDrive for Business / personal driveItem (no siteId, no downloadUrl)."""
    data: dict[str, Any] = {
        "id": OD_ITEM_ID,
        "name": "notes.docx",
        "size": 5,
        "webUrl": "https://onedrive.live.com/?cid=a1b2c3d4e5f60718&id=A1B2C3D4E5F60718%211234",
        "eTag": "aQTFCMkMzRDRFNUY2MDcxOCExMjM0LjA",
        "cTag": "aYzpBMUIyQzNENEU1RjYwNzE4ITEyMzQuMjU3",
        "createdDateTime": "2026-07-10T08:00:00Z",
        "lastModifiedDateTime": "2026-07-11T08:00:00Z",
        "createdBy": {"application": {"displayName": "OneDrive"}, "user": {"displayName": "Adele Vance"}},
        "lastModifiedBy": {"user": {"displayName": "Adele Vance"}},
        "parentReference": {"driveType": "personal", "driveId": OD_DRIVE_ID, "path": "/drive/root:/Docs"},
        "file": {"hashes": {"sha1Hash": "0123"}},
    }
    data.update(overrides)
    return data


def folder_item() -> dict[str, Any]:
    data = onedrive_item(name="Docs", id="A1B2C3D4E5F60718!99", size=2048, folder={"childCount": 3})
    del data["file"]
    return data


def graph_error(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)


def make_client(
    handler: Handler,
    *,
    settings: MicrosoftGraphSettings | None = None,
    token: str = "test-token",
    sleeps: list[float] | None = None,
) -> SharePointClient:
    """A SharePointClient whose HTTP traffic goes to `handler` via httpx.MockTransport."""
    recorder = sleeps if sleeps is not None else []
    return SharePointClient(
        StaticTokenAuthProvider(token),
        settings or graph_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=recorder.append,
    )


class FakeMsalApp:
    """Stand-in for msal.ConfidentialClientApplication."""

    def __init__(self, result: dict[str, Any], **init_kwargs: Any) -> None:
        self.result = result
        self.init_kwargs = init_kwargs
        self.calls: list[list[str]] = []

    def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
        self.calls.append(scopes)
        return self.result
