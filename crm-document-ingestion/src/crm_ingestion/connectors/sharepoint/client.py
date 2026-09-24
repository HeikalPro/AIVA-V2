"""Thin synchronous Microsoft Graph client for driveItem metadata and content."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote

import httpx

from crm_ingestion.config import MicrosoftGraphSettings
from crm_ingestion.connectors.sharepoint.auth import AuthenticationProvider
from crm_ingestion.connectors.sharepoint.models import DriveItem, DriveItemReference
from crm_ingestion.connectors.sharepoint.sharing import encode_sharing_url
from crm_ingestion.errors import DownloadError, GraphAPIError, GraphTransportError, ItemNotFoundError

MAX_RETRIES = 3
MAX_RETRY_DELAY_SECONDS = 60.0
_RETRY_STATUSES = frozenset({429, 503})


def _dig(data: Any, *keys: str) -> Any:
    cur: Any = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _display_name(identity_set: Any) -> str | None:
    for kind in ("user", "application", "device"):
        name = _dig(identity_set, kind, "displayName")
        if isinstance(name, str) and name:
            return name
    return None


def drive_item_from_graph(data: dict[str, Any]) -> DriveItem:
    """Map a Graph driveItem JSON object onto DriveItem."""
    parent = data.get("parentReference")
    parent = parent if isinstance(parent, dict) else {}
    drive_id = parent.get("driveId")
    if not data.get("id") or not drive_id:
        raise GraphAPIError(200, "driveItem response is missing id or parentReference.driveId")
    size = data.get("size")
    return DriveItem(
        id=data["id"],
        drive_id=drive_id,
        name=data.get("name") or data["id"],
        size=size if isinstance(size, int) else None,
        mime_type=_dig(data, "file", "mimeType"),
        web_url=data.get("webUrl"),
        etag=data.get("eTag"),
        ctag=data.get("cTag"),
        created_at=_parse_dt(data.get("createdDateTime")),
        modified_at=_parse_dt(data.get("lastModifiedDateTime")),
        created_by=_display_name(data.get("createdBy")),
        modified_by=_display_name(data.get("lastModifiedBy")),
        parent_path=parent.get("path"),
        site_id=parent.get("siteId"),
        is_file=data.get("file") is not None,
        sha256_hash=_dig(data, "file", "hashes", "sha256Hash"),
        quick_xor_hash=_dig(data, "file", "hashes", "quickXorHash"),
        download_url=data.get("@microsoft.graph.downloadUrl"),
        raw=data,
    )


def _error_from_response(response: httpx.Response) -> GraphAPIError:
    code: str | None = None
    message = response.reason_phrase or "request failed"
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or None
        message = error.get("message") or message
    elif response.text:
        message = response.text[:500]
    if response.status_code == 404:
        return ItemNotFoundError(404, message, code=code)
    return GraphAPIError(response.status_code, message, code=code)


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    header = response.headers.get("Retry-After")
    delay: float | None = None
    if header:
        try:
            delay = float(header)
        except ValueError:
            try:
                when = parsedate_to_datetime(header)
                delay = (when - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError):
                delay = None
    if delay is None:
        delay = float(2**attempt)
    return min(max(delay, 0.0), MAX_RETRY_DELAY_SECONDS)


def _seg(value: str) -> str:
    return quote(value, safe="!")


class SharePointClient:
    """Reads driveItems and their content from OneDrive / SharePoint via Microsoft Graph.

    Redirects are followed by httpx, which strips the Authorization header whenever a
    redirect leaves the Graph origin, so the bearer token never reaches the download host."""

    def __init__(
        self,
        auth: AuthenticationProvider,
        settings: MicrosoftGraphSettings | None = None,
        *,
        http_client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._auth = auth
        self._settings = settings or MicrosoftGraphSettings()
        self._base_url = self._settings.graph_base_url.rstrip("/")
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=self._settings.graph_timeout_seconds)
        self._sleep = sleep

    @property
    def settings(self) -> MicrosoftGraphSettings:
        return self._settings

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- metadata -------------------------------------------------------------------

    def get_drive_item(self, drive_id: str, item_id: str) -> DriveItem:
        """GET /drives/{drive_id}/items/{item_id}."""
        return self._get_json_item(f"/drives/{_seg(drive_id)}/items/{_seg(item_id)}")

    def resolve_sharing_url(self, url: str) -> DriveItem:
        """GET /shares/{encoded}/driveItem for a OneDrive/SharePoint sharing link."""
        return self._get_json_item(f"/shares/{encode_sharing_url(url)}/driveItem")

    def get_item(self, ref: DriveItemReference) -> DriveItem:
        """Resolve a reference by sharing URL or by drive/item ids."""
        if ref.sharing_url:
            return self.resolve_sharing_url(ref.sharing_url)
        if not (ref.drive_id and ref.item_id):  # unreachable: DriveItemReference validates this
            raise ValueError("reference needs a sharing_url or both drive_id and item_id")
        return self.get_drive_item(ref.drive_id, ref.item_id)

    def _get_json_item(self, path: str) -> DriveItem:
        try:
            response = self._send("GET", self._base_url + path, authenticated=True, stream=False)
        except httpx.TransportError as exc:
            raise GraphTransportError(f"network error calling Graph: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise GraphAPIError(response.status_code, "response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise GraphAPIError(response.status_code, "response is not a JSON object")
        return drive_item_from_graph(data)

    # --- content --------------------------------------------------------------------

    def download_content(self, item: DriveItem) -> bytes:
        """Download a file's bytes, enforcing settings.max_download_bytes."""
        if not item.is_file:
            raise DownloadError(f"{item.name!r} is a folder or other non-file item")
        limit = self._settings.max_download_bytes
        if item.size is not None and item.size > limit:
            raise DownloadError(f"{item.name!r} is {item.size} bytes, over the {limit}-byte limit")
        if item.download_url:
            url, authenticated = item.download_url, False
        else:
            url = f"{self._base_url}/drives/{_seg(item.drive_id)}/items/{_seg(item.id)}/content"
            authenticated = True
        try:
            response = self._send("GET", url, authenticated=authenticated, stream=True)
        except httpx.TransportError as exc:
            raise DownloadError(f"network error downloading {item.name!r}: {exc}") from exc
        except GraphAPIError as exc:
            raise DownloadError(f"could not download {item.name!r}: {exc}") from exc
        try:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise DownloadError(f"{item.name!r} is {declared} bytes, over the {limit}-byte limit")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise DownloadError(f"{item.name!r} exceeded the {limit}-byte limit while downloading")
                chunks.append(chunk)
            return b"".join(chunks)
        except httpx.TransportError as exc:
            raise DownloadError(f"network error downloading {item.name!r}: {exc}") from exc
        finally:
            response.close()

    # --- transport ------------------------------------------------------------------

    def _send(self, method: str, url: str, *, authenticated: bool, stream: bool) -> httpx.Response:
        """Send with one re-auth on 401 and bounded Retry-After handling on 429/503."""
        retries = 0
        reauthenticated = False
        while True:
            headers: dict[str, str] = {} if stream else {"Accept": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {self._auth.get_access_token()}"
            request = self._http.build_request(method, url, headers=headers)
            response = self._http.send(request, stream=stream, follow_redirects=True)
            status = response.status_code
            if status == 401 and authenticated and not reauthenticated:
                response.close()
                self._auth.invalidate()
                reauthenticated = True
                continue
            if status in _RETRY_STATUSES and retries < MAX_RETRIES:
                delay = _retry_delay(response, retries)
                response.close()
                self._sleep(delay)
                retries += 1
                continue
            if response.is_success:
                return response
            if stream:
                response.read()
            response.close()
            raise _error_from_response(response)
