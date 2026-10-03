"""Microsoft Graph (SharePoint / OneDrive) access for the sync.

Built on crm-document-ingestion's ``SharePointClient`` (site, library and path resolution,
paged folder listing, streamed downloads, 429/503 retry with Retry-After, one re-auth on
401, redirects that drop the bearer token when they leave the Graph host). Tokens come
from ``ClientCredentialsTokenProvider``: an app-only client-credentials grant over plain
httpx (no MSAL) that takes the DECRYPTED credentials of one source, never environment
variables. Blocking (httpx): call via asyncio.to_thread.

Every failure is a GraphFailed whose ``reason`` and ``suggested_action`` an admin can act on
(AADSTS codes, 401/403/404, throttling, network), scrubbed of the client secret and of
tokens. ``GraphFailed.code`` values: invalid_config, unavailable, auth_invalid_secret,
auth_expired_secret, auth_app_not_found, auth_tenant_not_found, auth_failed, forbidden,
not_found, throttled, network, too_large, storage_error, graph_error.

SSRF: the Graph base URL and the Entra ID authority come only from settings (https
required). A source's site URL contributes only a host (it must end with one of
DOC_INTEL_SHAREPOINT_HOST_SUFFIXES) and a path to a Graph request path; it is never
fetched itself. ``@odata.nextLink`` is followed only on the Graph origin.

Logging: httpx logs every request URL at INFO, and a SharePoint download URL carries a
short-lived ``tempauth`` token in its query string. A filter on the ``httpx`` logger
(installed once, by the first GraphSource) replaces such token values with ``<redacted>``.

crm-document-ingestion is optional: without it this module still imports (validation,
the data classes), ``graph_available()`` says why, and GraphSource calls fail with
code ``unavailable``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import quote, quote_plus, unquote, urlsplit

import httpx

from backend.doc_intel.settings import DocIntelSettings
from backend.doc_intel.textutil import iso_utc, utc_now

try:  # installed with the document-intelligence extras (backend/requirements-docintel.txt)
    from crm_ingestion.config import MicrosoftGraphSettings
    from crm_ingestion.connectors.sharepoint.auth import AuthenticationProvider
    from crm_ingestion.connectors.sharepoint.client import MAX_RETRIES, SharePointClient
    from crm_ingestion.connectors.sharepoint.models import DriveItem
    from crm_ingestion.errors import (
        DownloadError,
        DownloadLimitError,
        DriveNotFoundError,
        GraphAPIError,
        GraphTransportError,
        IngestionError,
    )

    _LIBRARY_ERROR: str | None = None
except Exception as _exc:  # missing or broken install: every GraphSource call reports "unavailable"
    _missing = getattr(_exc, "name", None) or ""
    if isinstance(_exc, ImportError) and (not _missing or _missing.startswith("crm_ingestion")):
        _LIBRARY_ERROR = "crm-document-ingestion is not installed in the backend environment — see the runbook"
    elif isinstance(_exc, ImportError):
        _LIBRARY_ERROR = f"crm-document-ingestion cannot be loaded: {_missing} is missing"
    else:
        _LIBRARY_ERROR = f"crm-document-ingestion cannot be imported ({type(_exc).__name__})"

    class _LibraryMissing(Exception):
        """Stands in for the library's exception types so ``except`` clauses stay valid."""

    AuthenticationProvider = object  # type: ignore[assignment,misc]
    MicrosoftGraphSettings = SharePointClient = DriveItem = None  # type: ignore[assignment,misc]
    MAX_RETRIES = 3
    DownloadError = DownloadLimitError = DriveNotFoundError = _LibraryMissing  # type: ignore[assignment,misc]
    GraphAPIError = GraphTransportError = IngestionError = _LibraryMissing  # type: ignore[assignment,misc]

_log = logging.getLogger(__name__)
_T = TypeVar("_T")

# ---- suggested actions ----------------------------------------------------------------------

_SYNC_PAGE = "the SharePoint Sync page"
ACTION_NEW_SECRET = (
    "Create a new client secret in Entra ID → App registrations → Certificates & secrets, "
    f"then paste it on {_SYNC_PAGE}"
)
ACTION_CHECK_SECRET = (
    "Copy the secret's Value (not its Secret ID) from Entra ID → App registrations → Certificates & "
    f"secrets, or create a new client secret there, then paste it on {_SYNC_PAGE}"
)
ACTION_CHECK_IDS = (
    f"Check the Tenant ID and Client ID on {_SYNC_PAGE} against Entra ID → App registrations → "
    "your app → Overview (Directory ID and Application ID)"
)
ACTION_CHECK_TENANT = (
    "Use the Directory (tenant) ID from Entra ID → Overview, or the tenant's domain "
    "such as contoso.onmicrosoft.com"
)
ACTION_CREDENTIALS = f"Enter the Tenant ID, Client ID and Client Secret on {_SYNC_PAGE}"
ACTION_PERMISSIONS = (
    "Grant Sites.Read.All + Files.Read.All (Application) and admin consent, "
    "or use Sites.Selected with a grant for this site"
)
ACTION_NETWORK = "Allow outbound HTTPS to login.microsoftonline.com and graph.microsoft.com"
ACTION_THROTTLED = "Wait a few minutes and try again; if it keeps happening, sync less often"
ACTION_SITE_URL = (
    "Check the site URL: use the site's address, e.g. https://contoso.sharepoint.com/sites/Sales "
    "(not a page, a library view or a sharing link), and that the app can access that site"
)
ACTION_LIBRARY = "Check the library name as shown on the site (e.g. Documents), or leave it empty for the default library"
ACTION_FOLDER = "Check the folder path inside the library (e.g. /CRM/Contracts), or leave it empty for the library root"
ACTION_INSTALL = (
    "Install crm-document-ingestion in the backend environment (see backend/requirements-docintel.txt), "
    "then restart the backend"
)
ACTION_ENDPOINTS = "Fix DOC_INTEL_GRAPH_BASE_URL / DOC_INTEL_GRAPH_AUTHORITY_HOST (https:// URLs) and restart"
ACTION_TOO_LARGE = "Split or compress the file in SharePoint, or raise DOC_INTEL_SYNC_MAX_FILE_MB"
ACTION_STORAGE = "Check the free disk space of the server's temporary directory, then sync again"
ACTION_RETRY_SYNC = "Run the sync again"

# AADSTS error code -> (GraphFailed code, reason, suggested action).
_AADSTS_MAPPED: dict[str, tuple[str, str, str]] = {
    "7000215": ("auth_invalid_secret", "Microsoft rejected the client secret (AADSTS7000215: invalid client secret)",
                ACTION_CHECK_SECRET),
    "7000222": ("auth_expired_secret", "The client secret has expired (AADSTS7000222)", ACTION_NEW_SECRET),
    "700016": ("auth_app_not_found", "No app with this Client ID exists in the tenant (AADSTS700016)",
               ACTION_CHECK_IDS),
    "90002": ("auth_tenant_not_found", "The tenant was not found (AADSTS90002)", ACTION_CHECK_TENANT),
    "900023": ("auth_tenant_not_found", "The Tenant ID is not a valid tenant identifier (AADSTS900023)",
               ACTION_CHECK_TENANT),
}
# Other well-known sign-in failures: still ``auth_failed``, with a more specific reason and action.
_AADSTS_OTHER: dict[str, tuple[str, str]] = {
    "7000218": ("The client secret was not sent (AADSTS7000218)", ACTION_CREDENTIALS),
    "7000112": ("The app is disabled in the tenant (AADSTS7000112)",
                "Enable the app in Entra ID → Enterprise applications → your app → Properties"),
    "53003": ("A Conditional Access policy blocked the app's sign-in (AADSTS53003)",
              "Ask the tenant admin to exclude this app from the Conditional Access policy"),
    "500011": ("Microsoft Graph is not available to this tenant at the configured cloud (AADSTS500011)",
               "Check DOC_INTEL_GRAPH_SCOPE and DOC_INTEL_GRAPH_AUTHORITY_HOST match the tenant's cloud"),
    "70011": ("The requested scope is invalid (AADSTS70011)",
              "Check DOC_INTEL_GRAPH_SCOPE (default https://graph.microsoft.com/.default)"),
}

_TOKEN_REFRESH_MARGIN_SECONDS = 300.0  # refresh 5 minutes before the token expires
_UNKNOWN_TOKEN_CACHE_SECONDS = 300.0
_PAGE_SIZE = 200
_MAX_SCANNED_ITEMS = 200_000  # a runaway library stops the listing (complete=False) rather than the server
_USER_AGENT = "NONISV|GoChat247|AIVA-DocIntel/1.0"  # Microsoft's recommended traffic decoration
_MB = 1024 * 1024

# test_connection() answers within the UI's (and nginx's 60 s) patience: no new step starts
# after the budget, and each request is capped, so the worst case is ~budget + one request.
_TEST_BUDGET_SECONDS = 35.0
_TEST_REQUEST_TIMEOUT_SECONDS = 10.0
_TEST_LISTING_SECONDS = 15.0
_TEST_MAX_FILES = 200
_TEST_SAMPLE_FILES = 5

_STEP_LABELS = {
    "credentials": "Credentials",
    "token": "Microsoft sign-in",
    "site": "SharePoint site",
    "drive": "Document library",
    "folder": "Folder",
    "listing": "File listing",
}
_WHAT_LABELS = {"site": "SharePoint site", "library": "document library", "folder": "folder",
                "listing": "folder listing", "file": "file"}

_GUID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
_DNS_NAME = re.compile(r"^(?=.{3,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_AADSTS_CODE = re.compile(r"AADSTS(\d+)")
_JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
# Characters SharePoint does not allow in file or folder names (":" would also end a Graph path).
_BAD_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f"*:<>?|]')
# Path segments that never belong to a site address (pages, layouts, APIs).
_NON_SITE_SEGMENTS = frozenset({"_layouts", "_api", "_vti_bin", "_forms"})
_MAX_SITE_URL_CHARS = 1000
_SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:tempauth|access_token|token|sig|signature|client_secret)=)[^&#\s\"']+")


class GraphFailed(Exception):
    def __init__(
        self,
        reason: str,
        *,
        code: str = "graph_error",
        suggested_action: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        # invalid_config | unavailable | auth_invalid_secret | auth_expired_secret | auth_app_not_found |
        # auth_tenant_not_found | auth_failed | forbidden | not_found | throttled | network |
        # too_large | storage_error | graph_error
        self.code = code
        self.suggested_action = suggested_action
        self.status_code = status_code


@dataclass(frozen=True)
class GraphCredentials:
    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class RemoteFile:
    drive_id: str
    item_id: str
    name: str
    path: str  # folder path inside the library, e.g. "/CRM/Contracts" ("/" for the library root)
    web_url: str | None
    size: int | None
    etag: str | None
    ctag: str | None
    quick_xor_hash: str | None
    modified_at: datetime | None  # naive UTC
    mime_type: str | None


@dataclass(frozen=True)
class ResolvedTarget:
    site_id: str
    drive_id: str
    folder_id: str


@dataclass
class ListingResult:
    files: list[RemoteFile]
    # False when the listing stopped early (max_files reached, an error mid-way):
    # the sync must then NOT mark unseen files as deleted.
    complete: bool
    truncated_reason: str | None = None


def graph_available() -> tuple[bool, str | None]:
    """(True, None) when crm-document-ingestion (the Graph connector) imported. Never raises."""
    return (_LIBRARY_ERROR is None), _LIBRARY_ERROR


# ---- validation ---------------------------------------------------------------------------


def validate_site_url(site_url: str, settings: DocIntelSettings) -> str:
    """Normalized https site URL whose host ends with one of settings.sharepoint_host_suffix_list.
    Raises GraphFailed(code="invalid_config") otherwise (SSRF guard).

    Normalized: scheme and host lower-case, no query, fragment or trailing slash, path
    segments percent-encoded consistently. Refused: other schemes, user names or passwords,
    ports, IP addresses and hosts outside the allowed suffixes, sharing links, and page or
    library-view URLs (``.aspx``, ``_layouts``)."""
    example = "https://contoso.sharepoint.com/sites/Sales"

    def invalid(reason: str) -> GraphFailed:
        return GraphFailed(reason, code="invalid_config", suggested_action=ACTION_SITE_URL)

    raw = site_url.strip() if isinstance(site_url, str) else ""
    if not raw:
        raise invalid(f"The SharePoint site URL is empty (for example {example})")
    if len(raw) > _MAX_SITE_URL_CHARS:
        raise invalid(f"The SharePoint site URL is longer than {_MAX_SITE_URL_CHARS} characters")
    if _CONTROL.search(raw) or "\\" in raw:
        raise invalid("The SharePoint site URL contains control characters or backslashes")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise invalid(f"The SharePoint site URL is not a valid URL (for example {example})") from None
    if parts.scheme.lower() != "https":
        raise invalid(f"The SharePoint site URL must start with https:// (for example {example})")
    if "@" in parts.netloc:
        raise invalid("The SharePoint site URL must not contain a user name or password")
    if port is not None or parts.netloc.endswith(":"):
        raise invalid("The SharePoint site URL must not contain a port")
    host = (parts.hostname or "").rstrip(".").lower()
    suffixes = _normalized_suffixes(settings.sharepoint_host_suffix_list)
    if not suffixes:  # fail closed: a misconfigured allow-list accepts nothing
        raise GraphFailed(
            "No SharePoint host suffixes are configured, so no site URL is accepted",
            code="invalid_config", suggested_action="Set DOC_INTEL_SHAREPOINT_HOST_SUFFIXES (default .sharepoint.com)",
        )
    if not _DNS_NAME.match(host) or not any(host.endswith(s) and len(host) > len(s) for s in suffixes):
        allowed = ", ".join(f"*{s}" for s in suffixes)
        raise invalid(f"The site must be on SharePoint Online ({allowed}), for example {example}")
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if segments and segments[0].startswith(":"):
        raise invalid(f"This is a sharing link, not a site address — paste the site URL, e.g. {example}")
    for segment in segments:
        lowered = segment.lower()
        if segment in (".", "..") or _CONTROL.search(segment) or "/" in segment or "\\" in segment:
            raise invalid("The SharePoint site URL contains an invalid path segment")
        if lowered in _NON_SITE_SEGMENTS or lowered.endswith(".aspx"):
            raise invalid(
                f"Paste the site's address only (for example {example}), not the address of a page or library view"
            )
    path = "".join("/" + quote(s, safe="") for s in segments)
    return f"https://{host}{path}"


def failed_connection_test(detail: str, suggested_action: str | None = None, *, key: str = "credentials") -> dict[str, Any]:
    """A ConnectionTestOut-shaped result with one failed step, for callers that cannot build a
    GraphSource at all (e.g. the stored credentials cannot be decrypted)."""
    return {
        "ok": False,
        "steps": [_step(key, False, None, detail, suggested_action)],
        "checked_at": iso_utc(utc_now()),
        "sample_files": [],
        "files_found": None,
    }


# ---- token provider -----------------------------------------------------------------------


class ClientCredentialsTokenProvider(AuthenticationProvider):  # type: ignore[misc,valid-type]
    """App-only Graph tokens from Entra ID's client-credentials grant, over plain httpx.

    ``POST {authority}/{tenant}/oauth2/v2.0/token`` with grant_type=client_credentials and
    scope=settings.graph_scope. The token is cached until 5 minutes before ``expires_in``;
    ``invalidate()`` (the library calls it once after a 401) drops it. Failures are
    GraphFailed with AADSTS-specific, scrubbed reasons."""

    def __init__(
        self,
        credentials: GraphCredentials,
        settings: DocIntelSettings,
        http: httpx.Client,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._authority = settings.graph_authority_host.rstrip("/")
        self._scope = settings.graph_scope
        self._http = http
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._valid_until = 0.0

    @property
    def token_url(self) -> str:
        return f"{self._authority}/{quote(self._credentials.tenant_id, safe='')}/oauth2/v2.0/token"

    def get_access_token(self) -> str:
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._valid_until:
                return self._token
            token, expires_in = self._request()
            self._token = token
            self._valid_until = now + _cache_seconds(expires_in)
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._valid_until = 0.0

    def scrub_values(self) -> tuple[str, ...]:
        """Strings that must never appear in a reason or a log line."""
        secret = self._credentials.client_secret
        values = [secret, quote_plus(secret), quote(secret, safe="")] if secret else []
        if self._token:
            values.append(self._token)
        return tuple(v for v in values if v)

    def _request(self) -> tuple[str, float | None]:
        try:
            response = self._http.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._credentials.client_id,
                    "client_secret": self._credentials.client_secret,
                    "scope": self._scope,
                },
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.TimeoutException:
            raise GraphFailed(
                f"Timed out connecting to Microsoft Entra ID ({_host_of(self._authority)})",
                code="network", suggested_action=ACTION_NETWORK,
            ) from None
        except httpx.HTTPError as exc:
            raise GraphFailed(
                f"Could not reach Microsoft Entra ID ({_host_of(self._authority)}): {type(exc).__name__}",
                code="network", suggested_action=ACTION_NETWORK,
            ) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            payload = None
        if response.status_code == 200 and payload is not None:
            token = payload.get("access_token")
            if isinstance(token, str) and token:
                return token, _seconds(payload.get("expires_in"))
            raise GraphFailed("Microsoft Entra ID answered without an access token", code="auth_failed",
                              suggested_action=ACTION_CREDENTIALS, status_code=200)
        raise _token_failure(response.status_code, payload, self.scrub_values())


def _cache_seconds(expires_in: float | None) -> float:
    if expires_in is None or expires_in <= 0:
        return _UNKNOWN_TOKEN_CACHE_SECONDS
    if expires_in > 2 * _TOKEN_REFRESH_MARGIN_SECONDS:
        return expires_in - _TOKEN_REFRESH_MARGIN_SECONDS
    return expires_in / 2


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _token_failure(status: int, payload: dict[str, Any] | None, secrets: tuple[str, ...]) -> GraphFailed:
    error = str(payload.get("error") or "") if payload else ""
    description = str(payload.get("error_description") or "") if payload else ""
    aadsts: str | None = None
    codes = payload.get("error_codes") if payload else None
    if isinstance(codes, list) and codes and isinstance(codes[0], int) and not isinstance(codes[0], bool):
        aadsts = str(codes[0])
    elif (match := _AADSTS_CODE.search(description)) is not None:
        aadsts = match.group(1)
    if aadsts in _AADSTS_MAPPED:
        code, reason, action = _AADSTS_MAPPED[aadsts]
        return GraphFailed(reason, code=code, suggested_action=action, status_code=status)
    if aadsts in _AADSTS_OTHER:
        reason, action = _AADSTS_OTHER[aadsts]
        return GraphFailed(reason, code="auth_failed", suggested_action=action, status_code=status)
    if status == 429:
        return GraphFailed("Microsoft Entra ID is throttling sign-in requests (HTTP 429)", code="throttled",
                           suggested_action=ACTION_THROTTLED, status_code=status)
    if status >= 500:
        return GraphFailed(f"Microsoft Entra ID is unavailable (HTTP {status}) — try again later",
                           code="auth_failed", suggested_action=ACTION_THROTTLED, status_code=status)
    first_line = _scrub(description.splitlines()[0] if description else "", secrets)
    if first_line:
        reason = f"Microsoft sign-in failed: {first_line}"
    elif payload is None:
        reason = (f"Microsoft Entra ID answered HTTP {status} without an error description "
                  "(a proxy or firewall may be intercepting the request)")
        return GraphFailed(reason, code="auth_failed", suggested_action=ACTION_NETWORK, status_code=status)
    else:
        reason = f"Microsoft sign-in failed (HTTP {status}, {_scrub(error, secrets, 80) or 'no error code'})"
    return GraphFailed(reason, code="auth_failed",
                       suggested_action=f"Check the Tenant ID, Client ID and Client Secret on {_SYNC_PAGE}",
                       status_code=status)


# ---- the Graph session --------------------------------------------------------------------


@dataclass
class _Listing:
    result: ListingResult
    error: GraphFailed | None = None  # set when an error stopped the listing (after something was listed)
    capped: bool = False  # stopped at max_files / the time limit / the scan limit


class GraphSource:
    """One source's Graph session (token cached until shortly before it expires)."""

    def __init__(self, credentials: GraphCredentials, settings: DocIntelSettings, *, transport: Any = None) -> None:
        """``transport``: an httpx transport for tests (httpx.MockTransport)."""
        self._credentials = GraphCredentials(
            tenant_id=(credentials.tenant_id or "").strip(),
            client_id=(credentials.client_id or "").strip(),
            client_secret=(credentials.client_secret or "").strip(),
        )
        self._settings = settings
        self._closed = False
        self._deadline: float | None = None  # test_connection: stop waiting for Retry-After past this
        self._config_error = _endpoint_problem(settings)
        self._http = httpx.Client(
            transport=transport,
            timeout=float(settings.graph_timeout_seconds),
            follow_redirects=False,  # the library follows redirects per request (and drops the bearer)
            trust_env=transport is None,  # tests: never a proxy from the environment
            headers={"User-Agent": _USER_AGENT},
        )
        self._auth = ClientCredentialsTokenProvider(self._credentials, settings, self._http)
        self._client_instance: Any = None
        _install_log_redaction()

    # ---- public API

    def acquire_token(self) -> None:
        """Get (or refresh) the app-only token. Raises GraphFailed with AADSTS-specific reasons."""
        self._check_ready()
        self._auth.get_access_token()

    def resolve(self, site_url: str, drive_name: str | None, folder_path: str | None) -> ResolvedTarget:
        """site URL -> site id; library name (None = the default library) -> drive id; folder path -> item id."""
        url = validate_site_url(site_url, self._settings)
        path = _normalize_folder_path(folder_path)  # invalid input fails before any request
        client = self._client()
        site = self._call(lambda: client.resolve_site(url), what="site", subject=url)
        drive = self._drive(client, site.id, drive_name)
        _, folder = self._folder(client, drive.id, path)
        return ResolvedTarget(site_id=site.id, drive_id=drive.id, folder_id=folder.id)

    def list_files(
        self,
        target: ResolvedTarget,
        *,
        recursive: bool,
        extensions: tuple[str, ...],
        max_files: int,
    ) -> ListingResult:
        """Paged (@odata.nextLink) listing of files under the folder, filtered by extension.

        - ``extensions`` match case-insensitively (".pdf" matches "A.PDF"); empty = every file.
        - Recursive listing is breadth first; ``RemoteFile.path`` is the containing folder's
          path inside the library.
        - Stops at ``max_files`` with complete=False and a truncated_reason.
        - An error after something was listed also returns complete=False with what was
          collected and the reason; an error before anything was listed raises GraphFailed.
        """
        return self._list(target, recursive=recursive, extensions=extensions, max_files=max_files).result

    def download(self, file: RemoteFile, dest: Path, *, max_bytes: int) -> str:
        """Stream the file content to ``dest`` (size capped); returns its sha256 hex.

        Written to ``<dest>.part`` first and renamed into place, so ``dest`` never holds a
        partial file; raises GraphFailed (too_large, not_found, forbidden, throttled, network,
        storage_error, ...)."""
        client = self._client()
        limit = int(max_bytes)
        if limit <= 0:
            raise ValueError("max_bytes must be positive")
        if file.size is not None and file.size > limit:
            raise _too_large(file.size, limit)
        item = DriveItem(id=file.item_id, drive_id=file.drive_id, name=file.name, size=file.size, is_file=True)
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        try:
            with open(part, "wb") as handle:

                def write(chunk: bytes) -> None:
                    handle.write(chunk)
                    digest.update(chunk)

                client.download_to(item, write, max_bytes=limit)
            os.replace(part, dest)
        except GraphFailed:
            raise
        except DownloadLimitError:
            raise _too_large(None, limit) from None
        except (IngestionError, httpx.HTTPError) as exc:
            raise self._failure(exc, what="file", subject=file.name) from None
        except OSError:
            raise GraphFailed("The downloaded file could not be written on the server", code="storage_error",
                              suggested_action=ACTION_STORAGE) from None
        finally:
            _remove_quietly(part)
        return digest.hexdigest()

    def test_connection(
        self,
        site_url: str,
        drive_name: str | None,
        folder_path: str | None,
        *,
        recursive: bool,
        extensions: tuple[str, ...],
    ) -> dict[str, Any]:
        """Step-by-step diagnostics shaped like schemas.ConnectionTestOut
        ({ok, steps[{key,label,ok,latency_ms,detail,suggested_action}], sample_files, files_found}).
        Never raises.

        Steps, in order: credentials, token, site, drive, folder, listing. The list ends at
        the first failed step (later steps did not run). Each request is capped at 10 s, a
        Retry-After wait never runs past the budget, the listing counts at most 200 matching
        files for at most ~15 s, and no step starts after 35 s: the test answers in ~45 s at worst."""
        steps: list[dict[str, Any]] = []
        result: dict[str, Any] = {"ok": False, "steps": steps, "checked_at": None, "sample_files": [],
                                  "files_found": None}
        saved_timeout = self._http.timeout
        self._deadline = time.monotonic() + _TEST_BUDGET_SECONDS
        try:
            limit = min(float(self._settings.graph_timeout_seconds), _TEST_REQUEST_TIMEOUT_SECONDS)
            self._http.timeout = httpx.Timeout(limit)
            self._run_test(steps, result, site_url, drive_name, folder_path, recursive=recursive,
                           extensions=extensions)
        except Exception as exc:  # the diagnostics must never fail the caller
            _log.warning("doc_intel: connection test failed unexpectedly: %s: %s", type(exc).__name__,
                         _scrub(exc, self._auth.scrub_values(), 200))
            steps.append(_step("unexpected", False, None,
                               f"The connection test failed unexpectedly ({type(exc).__name__}) — see the backend log",
                               None, label="Connection test"))
        finally:
            self._deadline = None
            try:
                self._http.timeout = saved_timeout
            except Exception:
                pass
        result["ok"] = bool(steps) and steps[-1]["key"] == "listing" and all(s["ok"] for s in steps)
        result["checked_at"] = iso_utc(utc_now())
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._http.close()
        except Exception:
            _log.debug("doc_intel: closing the Graph HTTP client failed", exc_info=True)

    def __enter__(self) -> GraphSource:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- internals

    def _check_ready(self) -> None:
        if self._closed:
            raise RuntimeError("GraphSource is closed")
        if _LIBRARY_ERROR is not None:
            raise GraphFailed(_LIBRARY_ERROR, code="unavailable", suggested_action=ACTION_INSTALL)
        if self._config_error is not None:
            raise self._config_error
        problem = _credentials_problem(self._credentials)
        if problem is not None:
            raise problem

    def _client(self) -> Any:
        self._check_ready()
        if self._client_instance is None:
            settings = self._settings
            # EVERY field explicit, so no MICROSOFT_* variable or .env file can leak in. The
            # credentials stay empty: this client never uses them (tokens come from self._auth).
            graph_settings = MicrosoftGraphSettings(
                _env_file=None,
                tenant_id="",
                client_id="",
                client_secret="",
                graph_base_url=settings.graph_base_url,
                authority_host=settings.graph_authority_host,
                graph_scope=settings.graph_scope,
                graph_timeout_seconds=float(settings.graph_timeout_seconds),
                max_download_bytes=settings.sync_max_file_bytes,
            )
            self._client_instance = SharePointClient(self._auth, graph_settings, http_client=self._http,
                                                     sleep=self._sleep)
        return self._client_instance

    def _sleep(self, seconds: float) -> None:
        """The library's Retry-After wait; a connection test stops waiting at its deadline."""
        if self._deadline is not None and time.monotonic() + seconds > self._deadline:
            raise GraphFailed("Microsoft Graph is throttling requests, so the connection test stopped waiting",
                              code="throttled", suggested_action=ACTION_THROTTLED, status_code=429)
        time.sleep(seconds)

    def _call(self, fn: Callable[[], _T], *, what: str, subject: str | None = None) -> _T:
        try:
            return fn()
        except GraphFailed:
            raise
        except (IngestionError, httpx.HTTPError, ValueError) as exc:
            raise self._failure(exc, what=what, subject=subject) from None

    def _drive(self, client: Any, site_id: str, drive_name: str | None) -> Any:
        name = (drive_name or "").strip() or None
        try:
            return client.get_drive(site_id, name)
        except DriveNotFoundError as exc:
            visible = ", ".join(exc.available) or "none visible to the app"
            raise GraphFailed(f"The document library '{name}' was not found on the site (libraries: {visible})",
                              code="not_found", suggested_action=ACTION_LIBRARY, status_code=404) from None
        except GraphFailed:
            raise
        except (IngestionError, httpx.HTTPError, ValueError) as exc:
            raise self._failure(exc, what="library", subject=name or "the default library") from None

    def _folder(self, client: Any, drive_id: str, folder_path: str | None) -> tuple[str, Any]:
        path = _normalize_folder_path(folder_path)
        item = self._call(lambda: client.get_item_by_path(drive_id, path), what="folder", subject=path)
        if not item.is_folder:
            raise GraphFailed(f"'{path}' is a file, not a folder", code="invalid_config",
                              suggested_action=ACTION_FOLDER)
        return path, item

    def _list(
        self,
        target: ResolvedTarget,
        *,
        recursive: bool,
        extensions: Iterable[str],
        max_files: int,
        deadline: float | None = None,
        start: Any = None,
    ) -> _Listing:
        client = self._client()
        exts = _normalize_extensions(extensions)
        limit = max(1, int(max_files))
        files: list[RemoteFile] = []
        # The start folder's path inside the drive: from its item when the caller has it, else from
        # its children's parentReference.path (no extra request; a missing folder is a 404 on the
        # first children page, which raises because nothing was listed yet).
        queue: deque[tuple[str, str | None]] = deque([(target.folder_id, _start_path(start) if start else None)])
        seen = {target.folder_id}
        scanned = 0
        listed_any = False
        folder_path: str | None = None
        try:
            while queue:
                folder_id, folder_path = queue.popleft()
                for item in client.iter_children(target.drive_id, folder_id, page_size=_PAGE_SIZE):
                    listed_any = True
                    scanned += 1
                    if folder_path is None:
                        folder_path = _parent_folder_path(item)
                    if scanned > _MAX_SCANNED_ITEMS:
                        reason = f"The listing stopped after looking at {_MAX_SCANNED_ITEMS} items"
                        return _Listing(ListingResult(files, False, reason), capped=True)
                    if deadline is not None and time.monotonic() > deadline:
                        return _Listing(ListingResult(files, False, "The listing stopped at its time limit"), capped=True)
                    if item.is_deleted:
                        continue
                    if item.is_folder:
                        if recursive and item.id not in seen:
                            seen.add(item.id)
                            queue.append((item.id, _join(folder_path, item.name)))
                        continue
                    if not item.is_file or (exts and not item.name.lower().endswith(exts)):
                        continue
                    if len(files) >= limit:
                        reason = f"Stopped at the limit of {limit} files (DOC_INTEL_SYNC_MAX_FILES)"
                        return _Listing(ListingResult(files, False, reason), capped=True)
                    files.append(_remote_file(item, folder_path, target.drive_id))
                listed_any = True  # a folder with no children was still listed
        except (GraphFailed, IngestionError, httpx.HTTPError) as exc:
            failure = exc if isinstance(exc, GraphFailed) else self._failure(exc, what="listing", subject=folder_path)
            if not listed_any:
                raise failure from None
            _log.warning("doc_intel: SharePoint listing stopped early after %d files (%s)", len(files), failure.code)
            return _Listing(ListingResult(files, False, f"The listing stopped early: {failure.reason}"), error=failure)
        return _Listing(ListingResult(files, True))

    def _run_test(
        self,
        steps: list[dict[str, Any]],
        result: dict[str, Any],
        site_url: str,
        drive_name: str | None,
        folder_path: str | None,
        *,
        recursive: bool,
        extensions: tuple[str, ...],
    ) -> None:
        def run(key: str, action: Callable[[], _T], describe: Callable[[_T], str]) -> tuple[bool, _T | None]:
            if self._deadline is not None and time.monotonic() > self._deadline:
                steps.append(_step(key, False, None,
                                   f"The connection test ran out of time ({_TEST_BUDGET_SECONDS:g} s) before this step",
                                   ACTION_NETWORK))
                return False, None
            started = time.perf_counter()
            try:
                value = action()
            except GraphFailed as exc:
                steps.append(_step(key, False, _ms(started), exc.reason, exc.suggested_action))
                return False, None
            steps.append(_step(key, True, _ms(started), describe(value), None))
            return True, value

        ok, _ = run("credentials", self._check_ready, lambda _: "Tenant ID, Client ID and Client Secret are set")
        if not ok:
            return
        ok, _ = run("token", self._auth.get_access_token,
                    lambda _: "Signed in to Microsoft Entra ID with the app's credentials")
        if not ok:
            return
        client = self._client()

        def site_step() -> Any:
            url = validate_site_url(site_url, self._settings)
            return self._call(lambda: client.resolve_site(url), what="site", subject=url)

        ok, site = run("site", site_step, lambda s: f"{s.display_name or s.name or 'Site'} ({s.web_url or site_url})")
        if not ok or site is None:
            return
        ok, drive = run("drive", lambda: self._drive(client, site.id, drive_name),
                        lambda d: f"Library '{d.name}'" + ("" if (drive_name or "").strip() else " (the site's default)"))
        if not ok or drive is None:
            return
        ok, folder = run("folder", lambda: self._folder(client, drive.id, folder_path), lambda f: f"Folder {f[0]}")
        if not ok or folder is None:
            return
        _, folder_item = folder
        exts = _normalize_extensions(extensions)
        remaining = (self._deadline - time.monotonic()) if self._deadline is not None else _TEST_LISTING_SECONDS
        deadline = time.monotonic() + max(1.0, min(_TEST_LISTING_SECONDS, remaining))
        target = ResolvedTarget(site_id=site.id, drive_id=drive.id, folder_id=folder_item.id)

        started = time.perf_counter()
        try:
            listing = self._list(target, recursive=recursive, extensions=exts, max_files=_TEST_MAX_FILES,
                                 deadline=deadline, start=folder_item)
        except GraphFailed as exc:
            steps.append(_step("listing", False, _ms(started), exc.reason, exc.suggested_action))
            return
        files = listing.result.files
        result["files_found"] = len(files)
        result["sample_files"] = [f.name for f in files[:_TEST_SAMPLE_FILES]]
        kinds = "/".join(e.lstrip(".").upper() for e in exts) or "files of any type"
        scope = " (including subfolders)" if recursive else ""
        if listing.error is not None:
            steps.append(_step("listing", False, _ms(started), listing.error.reason, listing.error.suggested_action))
        elif listing.capped:
            steps.append(_step("listing", True, _ms(started),
                               f"At least {len(files)} matching {kinds} found{scope}; the test stops counting there",
                               None))
        elif not files:
            steps.append(_step("listing", True, _ms(started), f"No {kinds} found in this folder{scope}",
                               "Check the folder path and the file types if files were expected here"))
        else:
            steps.append(_step("listing", True, _ms(started),
                               f"{len(files)} matching {kinds} found{scope}", None))

    def _failure(self, exc: BaseException, *, what: str, subject: str | None) -> GraphFailed:
        """Map a library/httpx exception to an admin-readable, scrubbed GraphFailed."""
        secrets = self._auth.scrub_values()
        label = _WHAT_LABELS.get(what, what)
        if isinstance(exc, GraphFailed):
            return exc
        if isinstance(exc, DownloadLimitError):
            return _too_large(None, getattr(exc, "limit", 0) or 0)
        if isinstance(exc, DownloadError) and exc.__cause__ is not None:
            return self._failure(exc.__cause__, what=what, subject=subject)
        cause = exc.__cause__ if isinstance(exc, GraphTransportError) else exc
        if isinstance(cause, httpx.TimeoutException):
            return GraphFailed(f"Timed out talking to Microsoft Graph while reading the {label}", code="network",
                               suggested_action=ACTION_NETWORK)
        if isinstance(exc, (GraphTransportError, httpx.TransportError)):
            name = type(cause).__name__ if cause is not None else "network error"
            return GraphFailed(f"Could not reach Microsoft Graph ({name})", code="network",
                               suggested_action=ACTION_NETWORK)
        if isinstance(exc, GraphAPIError):
            return self._status_failure(exc, what=what, label=label, subject=subject, secrets=secrets)
        if isinstance(exc, httpx.HTTPError):
            return GraphFailed(f"Microsoft Graph request failed ({type(exc).__name__})", code="graph_error",
                               suggested_action=ACTION_RETRY_SYNC)
        if isinstance(exc, DownloadError):
            return GraphFailed(f"The {label} could not be downloaded: {_scrub(exc, secrets)}", code="graph_error",
                               suggested_action=ACTION_RETRY_SYNC)
        if isinstance(exc, ValueError):
            return GraphFailed(f"Invalid {label} setting: {_scrub(exc, secrets)}", code="invalid_config",
                               suggested_action=_ACTION_FOR.get(what))
        return GraphFailed(f"Microsoft Graph request failed ({type(exc).__name__})", code="graph_error")

    def _status_failure(self, exc: Any, *, what: str, label: str, subject: str | None,
                        secrets: tuple[str, ...]) -> GraphFailed:
        status = int(getattr(exc, "status_code", 0) or 0)
        graph_code = getattr(exc, "code", None)
        tag = f"HTTP {status}" + (f", {_scrub(graph_code, secrets, 60)}" if graph_code else "")
        if status == 401:
            return GraphFailed(f"Microsoft Graph rejected the app's access token ({tag})", code="auth_failed",
                               suggested_action=ACTION_PERMISSIONS, status_code=status)
        if status == 403:
            return GraphFailed(f"Microsoft Graph denied access to the {label} ({tag})", code="forbidden",
                               suggested_action=ACTION_PERMISSIONS, status_code=status)
        if status == 404:
            return _not_found(what, subject, status)
        if status in (429, 503):
            return GraphFailed(
                f"Microsoft Graph is throttling requests ({tag}) and did not recover after {MAX_RETRIES} retries",
                code="throttled", suggested_action=ACTION_THROTTLED, status_code=status,
            )
        message = _scrub(str(exc), secrets, 200)
        if 400 <= status < 500:
            return GraphFailed(f"Microsoft Graph refused the request for the {label}: {message}", code="graph_error",
                               suggested_action=_ACTION_FOR.get(what), status_code=status)
        if status >= 500:
            return GraphFailed(f"Microsoft Graph failed while reading the {label} ({tag}) — try again later",
                               code="graph_error", suggested_action=ACTION_RETRY_SYNC, status_code=status)
        return GraphFailed(f"Microsoft Graph returned an unexpected response for the {label}: {message}",
                           code="graph_error", suggested_action=ACTION_RETRY_SYNC, status_code=status or None)


_ACTION_FOR = {"site": ACTION_SITE_URL, "library": ACTION_LIBRARY, "folder": ACTION_FOLDER,
               "listing": ACTION_FOLDER, "file": ACTION_RETRY_SYNC}


def _not_found(what: str, subject: str | None, status: int) -> GraphFailed:
    if what == "site":
        reason = f"The SharePoint site was not found: {subject}" if subject else "The SharePoint site was not found"
        return GraphFailed(reason, code="not_found", suggested_action=ACTION_SITE_URL, status_code=status)
    if what == "library":
        return GraphFailed(f"The document library was not found ({subject})", code="not_found",
                           suggested_action=ACTION_LIBRARY, status_code=status)
    if what == "file":
        return GraphFailed(f"The file '{subject}' no longer exists in SharePoint (deleted or moved since it was listed)",
                           code="not_found", suggested_action=ACTION_RETRY_SYNC, status_code=status)
    where = f"'{subject}' " if subject and subject.startswith("/") else ""
    return GraphFailed(f"The folder {where}was not found in the document library", code="not_found",
                       suggested_action=ACTION_FOLDER, status_code=status)


def _too_large(size: int | None, limit: int) -> GraphFailed:
    limit_text = f"{limit / _MB:g} MB" if limit else "the limit"
    if size is not None:
        reason = f"The file is {size / _MB:.1f} MB; the limit is {limit_text}"
    else:
        reason = f"The file is larger than the {limit_text} limit"
    return GraphFailed(reason, code="too_large", suggested_action=ACTION_TOO_LARGE)


def _credentials_problem(credentials: GraphCredentials) -> GraphFailed | None:
    missing = [label for label, value in (("Tenant ID", credentials.tenant_id), ("Client ID", credentials.client_id),
                                          ("Client Secret", credentials.client_secret)) if not value]
    if missing:
        verb = "is" if len(missing) == 1 else "are"
        return GraphFailed(f"{' and '.join(missing)} {verb} missing", code="invalid_config",
                           suggested_action=ACTION_CREDENTIALS)
    if not (_GUID.match(credentials.tenant_id) or _DNS_NAME.match(credentials.tenant_id.lower())):
        return GraphFailed("The Tenant ID must be a GUID (the Directory ID) or a domain such as contoso.onmicrosoft.com",
                           code="invalid_config", suggested_action=ACTION_CHECK_TENANT)
    if not _GUID.match(credentials.client_id):
        return GraphFailed("The Client ID must be the app's Application (client) ID, a GUID", code="invalid_config",
                           suggested_action=ACTION_CHECK_IDS)
    if _CONTROL.search(credentials.client_secret):
        return GraphFailed("The Client Secret contains control characters", code="invalid_config",
                           suggested_action=ACTION_CHECK_SECRET)
    return None


def _endpoint_problem(settings: DocIntelSettings) -> GraphFailed | None:
    for name, value in (("DOC_INTEL_GRAPH_BASE_URL", settings.graph_base_url),
                        ("DOC_INTEL_GRAPH_AUTHORITY_HOST", settings.graph_authority_host)):
        try:
            parts = urlsplit((value or "").strip())
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() != "https" or not parts.hostname or "@" in parts.netloc:
            return GraphFailed(f"{name} must be an https:// URL", code="invalid_config",
                               suggested_action=ACTION_ENDPOINTS)
    return None


def _normalized_suffixes(suffixes: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in suffixes:
        suffix = raw.strip().lower().rstrip(".")
        if not suffix:
            continue
        suffix = suffix if suffix.startswith(".") else "." + suffix  # "sharepoint.com" must not match "evilsharepoint.com"
        if suffix != "." and suffix not in out:
            out.append(suffix)
    return tuple(out)


def _normalize_folder_path(value: str | None) -> str:
    text = (value or "").strip().replace("\\", "/")
    segments = [s.strip() for s in text.split("/") if s.strip()]
    for segment in segments:
        if segment in (".", "..") or _BAD_NAME_CHARS.search(segment):
            raise GraphFailed(f"The folder path contains an invalid folder name: '{segment}'", code="invalid_config",
                              suggested_action=ACTION_FOLDER)
    return "/" + "/".join(segments)


def _normalize_extensions(extensions: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in extensions or ():
        ext = str(raw).strip().lower()
        if not ext:
            continue
        ext = ext if ext.startswith(".") else "." + ext
        if ext not in out:
            out.append(ext)
    return tuple(out)


def _start_path(item: Any) -> str:
    """A folder's own path inside the drive: its parent's path plus its name ("/" for the root)."""
    if item.is_root or not item.parent_path:
        return "/"
    return _join(_parent_folder_path(item), item.name)


def _parent_folder_path(item: Any) -> str:
    """The path of the folder holding ``item``, from parentReference.path ("/drives/<id>/root:/CRM"
    -> "/CRM"; percent-decoded, since Graph may encode it). "/" when unknown."""
    parent = str(item.parent_path or "")
    at = parent.find("root:")
    if at < 0:
        return "/"
    parts = [p for p in unquote(parent[at + len("root:"):]).split("/") if p]
    return "/" + "/".join(parts)


def _join(folder: str | None, name: str) -> str:
    return "/" + name if folder in (None, "", "/") else f"{folder.rstrip('/')}/{name}"


def _remote_file(item: Any, folder_path: str, drive_id: str) -> RemoteFile:
    return RemoteFile(
        drive_id=item.drive_id or drive_id,
        item_id=item.id,
        name=item.name,
        path=folder_path,
        web_url=item.web_url,
        size=item.size,
        etag=item.etag,
        ctag=item.ctag,
        quick_xor_hash=item.quick_xor_hash,
        modified_at=_naive_utc(item.modified_at),
        mime_type=item.mime_type,
    )


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _step(key: str, ok: bool, latency_ms: int | None, detail: str | None, suggested_action: str | None,
          *, label: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": label or _STEP_LABELS.get(key, key),
        "ok": ok,
        "latency_ms": latency_ms,
        "detail": detail,
        "suggested_action": suggested_action,
    }


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _scrub(text: object, secrets: tuple[str, ...], limit: int = 300) -> str:
    """One line, safe to show: the secret / token values, JWT-like strings and bearer tokens
    removed, control characters collapsed, truncated."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "***")
    value = _JWT_LIKE.sub("<token>", value)
    value = _BEARER.sub("Bearer <token>", value)
    value = " ".join(_CONTROL.sub(" ", value).split())
    if len(value) > limit:
        value = value[: max(1, limit - 1)].rstrip() + "…"
    return value


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or "the configured authority"
    except ValueError:
        return "the configured authority"


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---- log redaction ------------------------------------------------------------------------


class _RedactUrlTokens(logging.Filter):
    """Replaces token-bearing query values (``tempauth=``, ``access_token=``, ...) in the
    ``httpx`` logger's request lines with ``<redacted>``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and args:
            changed = False
            redacted: list[Any] = []
            for arg in args:
                if isinstance(arg, (str, httpx.URL)):
                    text = str(arg)
                    clean = _SENSITIVE_QUERY.sub(r"\1<redacted>", text)
                    if clean != text:
                        arg, changed = clean, True
                redacted.append(arg)
            if changed:
                record.args = tuple(redacted)
        elif isinstance(record.msg, str):
            record.msg = _SENSITIVE_QUERY.sub(r"\1<redacted>", record.msg)
        return True


_REDACTION_LOCK = threading.Lock()
_REDACTION_INSTALLED = False


def _install_log_redaction() -> None:
    global _REDACTION_INSTALLED
    with _REDACTION_LOCK:
        if _REDACTION_INSTALLED:
            return
        logging.getLogger("httpx").addFilter(_RedactUrlTokens())
        _REDACTION_INSTALLED = True
