"""Thin synchronous Microsoft Graph client for driveItem metadata and content, plus
site / document-library resolution and paged folder listing."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import quote, unquote, urlsplit

import httpx

from crm_ingestion.config import MicrosoftGraphSettings
from crm_ingestion.connectors.sharepoint.auth import AuthenticationProvider
from crm_ingestion.connectors.sharepoint.models import Drive, DriveItem, DriveItemReference, Site
from crm_ingestion.connectors.sharepoint.sharing import encode_sharing_url
from crm_ingestion.errors import (
    DownloadError,
    DownloadLimitError,
    DriveNotFoundError,
    GraphAPIError,
    GraphTransportError,
    ItemNotFoundError,
)

MAX_RETRIES = 3
MAX_RETRY_DELAY_SECONDS = 60.0
MAX_PAGE_SIZE = 999
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


def drive_item_from_graph(data: dict[str, Any], *, drive_id: str | None = None) -> DriveItem:
    """Map a Graph driveItem JSON object onto DriveItem.

    `drive_id` is used only when the response carries no parentReference.driveId (a
    caller that requested the item from a known drive); without it such a response is
    rejected."""
    parent = data.get("parentReference")
    parent = parent if isinstance(parent, dict) else {}
    drive_id = parent.get("driveId") or drive_id
    if not data.get("id") or not drive_id:
        raise GraphAPIError(200, "driveItem response is missing id or parentReference.driveId")
    size = data.get("size")
    folder = data.get("folder")
    child_count = folder.get("childCount") if isinstance(folder, dict) else None
    if not isinstance(child_count, int) or isinstance(child_count, bool):
        child_count = None
    parent_id = parent.get("id")
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
        is_folder=folder is not None,
        child_count=child_count,
        is_root=data.get("root") is not None,
        is_deleted=data.get("deleted") is not None,
        parent_id=parent_id if isinstance(parent_id, str) and parent_id else None,
    )


def site_from_graph(data: dict[str, Any]) -> Site:
    """Map a Graph site JSON object onto Site."""
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise GraphAPIError(200, "site response is missing id")
    hostname = _dig(data, "siteCollection", "hostname")
    return Site(
        id=data["id"],
        name=data.get("name"),
        display_name=data.get("displayName"),
        web_url=data.get("webUrl"),
        hostname=hostname if isinstance(hostname, str) else None,
        raw=data,
    )


def drive_from_graph(data: dict[str, Any], *, site_id: str | None = None) -> Drive:
    """Map a Graph drive JSON object onto Drive (`site_id`: the site it was read from)."""
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise GraphAPIError(200, "drive response is missing id")
    return Drive(
        id=data["id"],
        name=data.get("name") or data["id"],
        drive_type=data.get("driveType"),
        web_url=data.get("webUrl"),
        description=data.get("description") or None,
        site_id=site_id,
        raw=data,
    )


def _values(page: dict[str, Any]) -> list[dict[str, Any]]:
    """The objects of a Graph collection page ("value"); anything else is ignored."""
    value = page.get("value")
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def _url_leaf(url: str | None) -> str:
    """The last path segment of a URL, percent-decoded ("Shared Documents")."""
    if not url:
        return ""
    return unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])


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

    # --- sites, libraries and folders ------------------------------------------------

    def resolve_site(self, site_url: str) -> Site:
        """The site at `site_url` (e.g. https://contoso.sharepoint.com/sites/Sales):
        GET /sites/{hostname}:/{server-relative-path}, or /sites/{hostname} for the root site.

        Only the URL's host and path are used; they become part of the Graph request path,
        so the site URL itself is never requested."""
        parts = urlsplit(site_url.strip())
        host = (parts.hostname or "").rstrip(".")
        if parts.scheme.lower() != "https" or not host:
            raise ValueError("site_url must be an absolute https:// URL")
        segments = [unquote(s) for s in parts.path.split("/") if s]
        path = f"/sites/{_seg(host)}"
        if segments:
            path += ":/" + "/".join(_seg(s) for s in segments)
        return site_from_graph(self._get_json(self._base_url + path))

    def list_drives(self, site_id: str) -> list[Drive]:
        """GET /sites/{site_id}/drives, every page: the site's document libraries."""
        drives: list[Drive] = []
        for page in self._iter_pages(f"{self._base_url}/sites/{_seg(site_id)}/drives"):
            drives.extend(drive_from_graph(d, site_id=site_id) for d in _values(page))
        return drives

    def get_drive(self, site_id: str, name: str | None = None) -> Drive:
        """The site's document library `name`; its default library (GET /sites/{site_id}/drive)
        when `name` is None or blank.

        `name` matches a library's display name ("Documents") or the last segment of its URL
        ("Shared Documents"), case-insensitively. No match raises DriveNotFoundError (an
        ItemNotFoundError listing the libraries the app can see)."""
        wanted = (name or "").strip()
        if not wanted:
            data = self._get_json(f"{self._base_url}/sites/{_seg(site_id)}/drive")
            return drive_from_graph(data, site_id=site_id)
        drives = self.list_drives(site_id)
        key = wanted.casefold()
        match = next((d for d in drives if d.name.casefold() == key), None)
        if match is None:
            match = next((d for d in drives if _url_leaf(d.web_url).casefold() == key), None)
        if match is None:
            raise DriveNotFoundError(wanted, [d.name for d in drives])
        return match

    def get_item_by_path(self, drive_id: str, path: str | None) -> DriveItem:
        """The item at `path` inside the drive ("/CRM/Contracts"): GET /drives/{drive_id}/root:/{path}.
        "/", "" or None is the drive's root folder (GET /drives/{drive_id}/root)."""
        segments = [s for s in (path or "").split("/") if s]
        url = f"{self._base_url}/drives/{_seg(drive_id)}/root"
        if segments:
            url += ":/" + "/".join(_seg(s) for s in segments)
        return drive_item_from_graph(self._get_json(url), drive_id=drive_id)

    def iter_children(self, drive_id: str, item_id: str, *, page_size: int = 200) -> Iterator[DriveItem]:
        """The children of a folder (GET /drives/{drive_id}/items/{item_id}/children?$top=N),
        following @odata.nextLink through the same retry path as every other request.

        Items are yielded page by page, so an error on a later page is raised after the
        earlier pages' items were yielded. A nextLink that leaves the Graph origin is
        refused with GraphAPIError: the bearer token only ever goes to graph_base_url's host."""
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        url = f"{self._base_url}/drives/{_seg(drive_id)}/items/{_seg(item_id)}/children?$top={page_size}"
        for page in self._iter_pages(url):
            for entry in _values(page):
                yield drive_item_from_graph(entry, drive_id=drive_id)

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

    def download_to(
        self, item: DriveItem, write: Callable[[bytes], object], *, max_bytes: int | None = None
    ) -> int:
        """Stream a file's bytes into `write` (e.g. an open file's ``write``) without holding
        the file in memory; returns the number of bytes written.

        Same URL choice, bearer handling and errors as `download_content`. Going over
        `max_bytes` (default settings.max_download_bytes) raises DownloadLimitError, a
        DownloadError. Bytes already written when an error is raised are the caller's to discard."""
        if not item.is_file:
            raise DownloadError(f"{item.name!r} is a folder or other non-file item")
        limit = self._settings.max_download_bytes if max_bytes is None else max_bytes
        if limit <= 0:
            raise ValueError("max_bytes must be positive")
        if item.size is not None and item.size > limit:
            raise DownloadLimitError(
                f"{item.name!r} is {item.size} bytes, over the {limit}-byte limit", limit=limit
            )
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
                raise DownloadLimitError(
                    f"{item.name!r} is {declared} bytes, over the {limit}-byte limit", limit=limit
                )
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > limit:
                    raise DownloadLimitError(
                        f"{item.name!r} exceeded the {limit}-byte limit while downloading", limit=limit
                    )
                write(chunk)
            return total
        except httpx.TransportError as exc:
            raise DownloadError(f"network error downloading {item.name!r}: {exc}") from exc
        finally:
            response.close()

    # --- transport ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict[str, Any]:
        """GET a Graph URL and return its JSON object (transport errors as GraphTransportError)."""
        try:
            response = self._send("GET", url, authenticated=True, stream=False)
        except httpx.TransportError as exc:
            raise GraphTransportError(f"network error calling Graph: {exc}") from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise GraphAPIError(response.status_code, "response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise GraphAPIError(response.status_code, "response is not a JSON object")
        return data

    def _iter_pages(self, url: str) -> Iterator[dict[str, Any]]:
        """Each page of a Graph collection, following @odata.nextLink (same origin only)."""
        next_url: str | None = url
        while next_url:
            page = self._get_json(next_url)
            yield page
            link = page.get("@odata.nextLink")
            next_url = self._same_origin(link) if isinstance(link, str) and link else None

    def _same_origin(self, url: str) -> str:
        """`url` when it has graph_base_url's scheme, host and port; GraphAPIError otherwise."""
        try:
            target, base = httpx.URL(url), httpx.URL(self._base_url)
        except httpx.InvalidURL as exc:
            raise GraphAPIError(200, "invalid @odata.nextLink") from exc
        if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
            raise GraphAPIError(
                200, f"refusing to follow an @odata.nextLink to another host ({target.host!r})"
            )
        return url

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
