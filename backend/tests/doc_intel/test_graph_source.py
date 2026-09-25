"""Microsoft Graph access for the SharePoint sync (backend.doc_intel.graph_source).

Both the Entra ID token endpoint and Graph are played by one httpx.MockTransport fake, so
nothing here reaches Microsoft or any other service. Every identifier, secret and token is
obviously fake. No database.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest

from backend.doc_intel import graph_source
from backend.doc_intel.graph_source import (
    ACTION_NETWORK,
    ACTION_PERMISSIONS,
    GraphCredentials,
    GraphFailed,
    GraphSource,
    ListingResult,
    RemoteFile,
    ResolvedTarget,
    failed_connection_test,
    graph_available,
    validate_site_url,
)
from backend.doc_intel.settings import DocIntelSettings

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "66666666-7777-8888-9999-000000000000"
SECRET = "FAKE~client.secret_for-tests-9Zq4"
TOKEN = "fake-access-token-0001"
HOST = "contoso.sharepoint.com"
SITE_URL = f"https://{HOST}/sites/Sales"
SITE_ID = f"{HOST},aaaaaaaa-0000-4000-8000-000000000001,bbbbbbbb-0000-4000-8000-000000000002"
DRIVE_ID = "b!fake-drive-documents"
OTHER_DRIVE_ID = "b!fake-drive-contracts"
ROOT_ID, CRM_ID, SUB_ID, DEEP_ID = "ITEM-ROOT", "ITEM-CRM", "ITEM-SUB", "ITEM-DEEP"
GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"
TEMPAUTH = "FAKE-TEMPAUTH-VALUE-7788"
ARABIC_ENC = "%D8%B9%D9%82%D9%88%D8%AF"  # "عقود", percent-encoded
CONTENT = b"%PDF-1.7 fake file content for the download test\n" * 50

Handler = Callable[[httpx.Request], httpx.Response]


# ---- the fake Microsoft ---------------------------------------------------------------------


def _item(item_id: str, name: str, parent: str, *, folder: bool = False, size: int | None = 10,
          **extra) -> dict:
    """A driveItem in ``parent`` ("/" or "/CRM/Sub")."""
    parent_rel = "" if parent == "/" else parent
    data = {
        "id": item_id,
        "name": name,
        "size": size,
        "eTag": f'"{{{item_id}}},1"',
        "cTag": f'"c:{{{item_id}}},2"',
        "createdDateTime": "2026-08-01T09:15:00Z",
        "lastModifiedDateTime": "2026-09-20T14:30:45Z",
        "webUrl": f"https://{HOST}/sites/Sales/Shared%20Documents{parent_rel}/{name}",
        "parentReference": {"driveId": DRIVE_ID, "id": f"parent-of-{item_id}",
                            "path": f"/drives/{DRIVE_ID}/root:{parent_rel}"},
    }
    if folder:
        data["folder"] = {"childCount": 3}
    else:
        data["file"] = {"mimeType": "application/pdf", "hashes": {"quickXorHash": f"qx-{item_id}"}}
    data.update(extra)
    return data


def _root(item_id: str, drive_id: str) -> dict:
    """A drive's root folder: a "root" facet and no parentReference.path."""
    return {"id": item_id, "name": "root", "root": {}, "folder": {"childCount": 2},
            "webUrl": f"https://{HOST}/sites/Sales/Shared%20Documents",
            "parentReference": {"driveId": drive_id, "driveType": "documentLibrary"}}


def _notebook(item_id: str, name: str, parent: str) -> dict:
    """A OneNote notebook: a "package" facet, neither a file nor a folder."""
    data = _item(item_id, name, parent)
    del data["file"]
    data["package"] = {"type": "oneNote"}
    return data


ROOT_ITEM = _root(ROOT_ID, DRIVE_ID)
OTHER_ROOT_ITEM = _root("ITEM-OTHER-ROOT", OTHER_DRIVE_ID)
CRM_ITEM = _item(CRM_ID, "CRM", "/", folder=True)
CHILDREN = {
    ROOT_ID: [[CRM_ITEM, _item("ITEM-ROOTFILE", "root.pdf", "/")]],
    CRM_ID: [
        [_item("ITEM-A", "a.pdf", "/CRM"), _item("ITEM-B", "B.PDF", "/CRM"), _item("ITEM-TXT", "notes.txt", "/CRM"),
         _item(SUB_ID, "Sub", "/CRM", folder=True)],
        [_item("ITEM-E", "e.docx", "/CRM"), _item("ITEM-GONE", "gone.pdf", "/CRM", deleted={"state": "deleted"}),
         _notebook("ITEM-NB", "Notebook", "/CRM")],
    ],
    SUB_ID: [[_item("ITEM-C", "c.docx", "/CRM/Sub"), _item("ITEM-OLD", "old.doc", "/CRM/Sub"),
              _item(DEEP_ID, "Deep", "/CRM/Sub", folder=True)]],
    DEEP_ID: [[_item("ITEM-D", "d.pdf", "/CRM/Sub/Deep")]],
}
ITEMS = {i["id"]: i for pages in CHILDREN.values() for page in pages for i in page}
ITEMS[ROOT_ID] = ROOT_ITEM


def graph_error(status: int, code: str, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)


def token_error(code: int, *, status: int = 400, error: str = "invalid_client", with_codes: bool = True) -> Handler:
    description = (f"AADSTS{code}: Something failed for the secret {SECRET} of app '{CLIENT}'.\r\n"
                   "Trace ID: 0000\r\nCorrelation ID: 1111\r\nTimestamp: 2026-09-25 10:00:00Z")
    body = {"error": error, "error_description": description, "trace_id": "0000"}
    if with_codes:
        body["error_codes"] = [code]
    return lambda request: httpx.Response(status, json=body)


class FakeMicrosoft:
    """Entra ID's token endpoint plus the Graph routes the sync uses, with overridable routes."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens = [TOKEN]  # issued in order; the last one repeats
        self.token_handler: Handler | None = None
        self.overrides: dict[tuple[str, str], Handler] = {}

    # -- helpers for assertions
    def graph_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "graph.microsoft.com"]

    def token_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "login.microsoftonline.com"]

    def override(self, method: str, path: str, handler: Handler) -> None:
        self.overrides[(method, path)] = handler

    # -- routing
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        custom = self.overrides.get((request.method, path))
        if custom is not None:
            return custom(request)
        if request.url.host == "login.microsoftonline.com":
            return self._token(request)
        if request.url.host == HOST and path.endswith("/download.aspx"):
            assert "authorization" not in request.headers, "the bearer token must not reach the download host"
            return httpx.Response(200, content=CONTENT)
        if request.url.host != "graph.microsoft.com":
            raise AssertionError(f"unexpected host {request.url.host}")
        if request.headers.get("authorization") not in {f"Bearer {t}" for t in self.tokens}:
            return graph_error(401, "InvalidAuthenticationToken", "Access token is empty or invalid")
        return self._graph(request, path)

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_handler is not None:
            return self.token_handler(request)
        issued = len(self.token_requests()) - 1
        token = self.tokens[min(issued, len(self.tokens) - 1)]
        return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599,
                                         "access_token": token})

    def _graph(self, request: httpx.Request, path: str) -> httpx.Response:
        v1 = "/v1.0"
        if path == f"{v1}/sites/{HOST}:/sites/Sales":
            return httpx.Response(200, json={"id": SITE_ID, "name": "Sales", "displayName": "Sales Team",
                                             "webUrl": SITE_URL, "siteCollection": {"hostname": HOST}})
        if path == f"{v1}/sites/{SITE_ID}/drive":
            return httpx.Response(200, json=self._drive(DRIVE_ID, "Documents", "Shared%20Documents"))
        if path == f"{v1}/sites/{SITE_ID}/drives":
            return httpx.Response(200, json={"value": [self._drive(DRIVE_ID, "Documents", "Shared%20Documents"),
                                                       self._drive(OTHER_DRIVE_ID, "Contracts", "Contracts")]})
        if path == f"{v1}/drives/{DRIVE_ID}/root":
            return httpx.Response(200, json=ROOT_ITEM)
        if path == f"{v1}/drives/{OTHER_DRIVE_ID}/root":
            return httpx.Response(200, json=OTHER_ROOT_ITEM)
        if path == f"{v1}/drives/{DRIVE_ID}/root:/CRM":
            return httpx.Response(200, json=CRM_ITEM)
        if path == f"{v1}/drives/{DRIVE_ID}/root:/CRM/a.pdf":
            return httpx.Response(200, json=ITEMS["ITEM-A"])
        prefix = f"{v1}/drives/{DRIVE_ID}/items/"
        if path.startswith(prefix):
            rest = path[len(prefix):]
            if rest.endswith("/children"):
                return self._children(request, rest[: -len("/children")])
            if rest.endswith("/content"):
                location = f"https://{HOST}/sites/Sales/_layouts/15/download.aspx?UniqueId=u1&tempauth={TEMPAUTH}"
                return httpx.Response(302, headers={"Location": location})
            if rest in ITEMS:
                return httpx.Response(200, json=ITEMS[rest])
        return graph_error(404, "itemNotFound", "The resource could not be found.")

    def _children(self, request: httpx.Request, item_id: str) -> httpx.Response:
        pages = CHILDREN.get(item_id)
        if pages is None:
            return graph_error(404, "itemNotFound", "The resource could not be found.")
        assert request.url.params.get("$top") == "200"
        index = int(request.url.params.get("$skiptoken", "0"))
        body: dict = {"value": pages[index]}
        if index + 1 < len(pages):
            body["@odata.nextLink"] = f"{GRAPH}/drives/{DRIVE_ID}/items/{item_id}/children?$top=200&$skiptoken={index + 1}"
        return httpx.Response(200, json=body)

    @staticmethod
    def _drive(drive_id: str, name: str, leaf: str) -> dict:
        return {"id": drive_id, "name": name, "driveType": "documentLibrary",
                "webUrl": f"https://{HOST}/sites/Sales/{leaf}"}


@pytest.fixture
def fake() -> FakeMicrosoft:
    return FakeMicrosoft()


def make_settings(**overrides) -> DocIntelSettings:
    values = {"graph_timeout_seconds": 5.0}
    values.update(overrides)
    return DocIntelSettings(_env_file=None, **values)


def make_source(fake: FakeMicrosoft, *, secret: str = SECRET, tenant: str = TENANT, client: str = CLIENT,
                settings: DocIntelSettings | None = None) -> GraphSource:
    return GraphSource(GraphCredentials(tenant, client, secret), settings or make_settings(),
                       transport=httpx.MockTransport(fake))


def failure_of(action: Callable[[], object]) -> GraphFailed:
    with pytest.raises(GraphFailed) as info:
        action()
    return info.value


def target(folder_id: str = CRM_ID) -> ResolvedTarget:
    return ResolvedTarget(site_id=SITE_ID, drive_id=DRIVE_ID, folder_id=folder_id)


EXTS = (".pdf", ".docx")


# ---- resolution ------------------------------------------------------------------------------


def test_resolve_site_named_library_and_folder(fake):
    with make_source(fake) as source:
        resolved = source.resolve(SITE_URL, "Documents", "/CRM")
    assert resolved == ResolvedTarget(site_id=SITE_ID, drive_id=DRIVE_ID, folder_id=CRM_ID)

    (token_request,) = fake.token_requests()
    assert str(token_request.url) == TOKEN_URL
    form = parse_qs(token_request.content.decode())
    assert form == {"grant_type": ["client_credentials"], "client_id": [CLIENT], "client_secret": [SECRET],
                    "scope": ["https://graph.microsoft.com/.default"]}
    paths = [r.url.path for r in fake.graph_requests()]
    assert paths == [f"/v1.0/sites/{HOST}:/sites/Sales", f"/v1.0/sites/{SITE_ID}/drives",
                     f"/v1.0/drives/{DRIVE_ID}/root:/CRM"]
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in fake.graph_requests())


def test_resolve_defaults_to_the_default_library_and_its_root(fake):
    with make_source(fake) as source:
        for folder in (None, "", "/", " // "):
            assert source.resolve(SITE_URL + "/", None, folder) == target(ROOT_ID)
    paths = {r.url.path for r in fake.graph_requests()}
    assert f"/v1.0/sites/{SITE_ID}/drive" in paths and f"/v1.0/drives/{DRIVE_ID}/root" in paths
    assert len(fake.token_requests()) == 1  # cached across calls


def test_resolve_library_by_url_segment_and_case(fake):
    with make_source(fake) as source:
        assert source.resolve(SITE_URL, "shared documents", "CRM") == target(CRM_ID)
        assert source.resolve(SITE_URL, " CONTRACTS ", None) == ResolvedTarget(
            site_id=SITE_ID, drive_id=OTHER_DRIVE_ID, folder_id="ITEM-OTHER-ROOT")


def test_resolve_names_the_missing_part(fake):
    source = make_source(fake)
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Nope",
                  lambda r: graph_error(404, "itemNotFound", "Requested site could not be found"))
    site = failure_of(lambda: source.resolve(f"https://{HOST}/sites/Nope", None, None))
    assert (site.code, site.status_code) == ("not_found", 404)
    assert "SharePoint site was not found" in site.reason and "/sites/Nope" in site.reason

    library = failure_of(lambda: source.resolve(SITE_URL, "Invoices", None))
    assert library.code == "not_found"
    assert "'Invoices'" in library.reason and "Documents, Contracts" in library.reason
    assert "library" in library.suggested_action

    folder = failure_of(lambda: source.resolve(SITE_URL, None, "/CRM/Missing"))
    assert folder.code == "not_found" and "folder '/CRM/Missing'" in folder.reason
    assert "folder path" in folder.suggested_action

    a_file = failure_of(lambda: source.resolve(SITE_URL, None, "/CRM/a.pdf"))
    assert a_file.code == "invalid_config" and "is a file" in a_file.reason


@pytest.mark.parametrize("folder", ["/CRM/../HR", "CRM/./x", "CRM/a:b", "CRM/what?", "CRM/a|b"])
def test_resolve_rejects_bad_folder_paths_before_any_request(fake, folder):
    failure = failure_of(lambda: make_source(fake).resolve(SITE_URL, None, folder))
    assert failure.code == "invalid_config" and "folder" in failure.reason
    assert fake.requests == []


def test_resolve_rejects_a_bad_site_url_before_any_request(fake):
    failure = failure_of(lambda: make_source(fake).resolve("http://contoso.sharepoint.com/sites/Sales", None, None))
    assert failure.code == "invalid_config"
    assert fake.requests == []


# ---- listing ---------------------------------------------------------------------------------


def test_list_files_recursive_paged_and_filtered(fake):
    with make_source(fake) as source:
        listing = source.list_files(target(), recursive=True, extensions=EXTS, max_files=100)
    assert isinstance(listing, ListingResult) and listing.complete and listing.truncated_reason is None
    assert [(f.name, f.path) for f in listing.files] == [
        ("a.pdf", "/CRM"), ("B.PDF", "/CRM"), ("e.docx", "/CRM"), ("c.docx", "/CRM/Sub"), ("d.pdf", "/CRM/Sub/Deep"),
    ]
    first = listing.files[0]
    assert first == RemoteFile(
        drive_id=DRIVE_ID, item_id="ITEM-A", name="a.pdf", path="/CRM",
        web_url=f"https://{HOST}/sites/Sales/Shared%20Documents/CRM/a.pdf", size=10,
        etag='"{ITEM-A},1"', ctag='"c:{ITEM-A},2"', quick_xor_hash="qx-ITEM-A",
        modified_at=datetime(2026, 9, 20, 14, 30, 45), mime_type="application/pdf",
    )
    assert first.modified_at.tzinfo is None  # naive UTC
    # only children pages are requested: CRM page 1 + its nextLink, then Sub and Deep
    assert all(r.url.path.endswith("/children") for r in fake.graph_requests())
    skip_tokens = [r.url.params.get("$skiptoken") for r in fake.graph_requests()]
    assert skip_tokens == [None, "1", None, None]


def test_folder_paths_come_decoded_from_parent_references(fake):
    encoded = f"/drives/{DRIVE_ID}/root:/CRM/Q3%20Reports%20%23{ARABIC_ENC}"
    child = _item("ITEM-Q3", "q3.pdf", "/CRM")
    child["parentReference"] = {**child["parentReference"], "path": encoded}
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/ITEM-Q3FOLDER/children",
                  lambda r: httpx.Response(200, json={"value": [child]}))
    listing = make_source(fake).list_files(target("ITEM-Q3FOLDER"), recursive=True, extensions=EXTS, max_files=9)
    assert [(f.name, f.path) for f in listing.files] == [("q3.pdf", "/CRM/Q3 Reports #عقود")]
    empty = make_source(FakeMicrosoft())
    empty_fake_listing = empty.list_files(target(DEEP_ID), recursive=False, extensions=(".docx",), max_files=9)
    assert empty_fake_listing.files == [] and empty_fake_listing.complete is True


def test_list_files_not_recursive(fake):
    listing = make_source(fake).list_files(target(), recursive=False, extensions=EXTS, max_files=100)
    assert [f.name for f in listing.files] == ["a.pdf", "B.PDF", "e.docx"] and listing.complete
    assert not any(SUB_ID in r.url.path for r in fake.graph_requests())


def test_extension_filter_is_case_insensitive_and_normalized(fake):
    source = make_source(fake)
    pdfs = source.list_files(target(), recursive=True, extensions=("PDF",), max_files=100)
    assert [f.name for f in pdfs.files] == ["a.pdf", "B.PDF", "d.pdf"]
    docx = source.list_files(target(), recursive=True, extensions=(" .DOCX ",), max_files=100)
    assert [f.name for f in docx.files] == ["e.docx", "c.docx"]
    everything = source.list_files(target(), recursive=True, extensions=(), max_files=100)
    # deleted items and OneNote packages are never files to sync
    assert [f.name for f in everything.files] == ["a.pdf", "B.PDF", "notes.txt", "e.docx", "c.docx", "old.doc", "d.pdf"]


def test_listing_from_the_library_root_uses_root_relative_paths(fake):
    listing = make_source(fake).list_files(target(ROOT_ID), recursive=True, extensions=(".pdf",), max_files=100)
    assert [(f.name, f.path) for f in listing.files] == [
        ("root.pdf", "/"), ("a.pdf", "/CRM"), ("B.PDF", "/CRM"), ("d.pdf", "/CRM/Sub/Deep"),
    ]


def test_max_files_truncates_with_complete_false(fake):
    source = make_source(fake)
    capped = source.list_files(target(), recursive=True, extensions=EXTS, max_files=2)
    assert [f.name for f in capped.files] == ["a.pdf", "B.PDF"]
    assert capped.complete is False and "limit of 2 files" in capped.truncated_reason
    exact = source.list_files(target(), recursive=True, extensions=EXTS, max_files=5)
    assert len(exact.files) == 5 and exact.complete is True  # exactly the limit is still complete


def test_the_scan_cap_stops_a_runaway_listing_as_incomplete(fake, monkeypatch):
    """Review Phase 2: items that are not collected (other types, folders) still count toward the
    scan cap (200 000 in production), so a huge library ends as complete=False, never deletes."""
    monkeypatch.setattr(graph_source, "_MAX_SCANNED_ITEMS", 4)
    listing = make_source(fake).list_files(target(), recursive=True, extensions=EXTS, max_files=100)
    assert [f.name for f in listing.files] == ["a.pdf", "B.PDF"]  # notes.txt and the Sub folder were scanned too
    assert listing.complete is False and listing.truncated_reason == "The listing stopped after looking at 4 items"


@pytest.mark.parametrize(
    "response, expected",
    [
        (lambda r: graph_error(500, "generalException", "boom"), "HTTP 500"),
        (lambda r: graph_error(403, "accessDenied", "Access denied"), "denied access"),
    ],
)
def test_an_error_mid_listing_returns_what_was_collected(fake, response, expected):
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{SUB_ID}/children", response)
    listing = make_source(fake).list_files(target(), recursive=True, extensions=EXTS, max_files=100)
    assert [f.name for f in listing.files] == ["a.pdf", "B.PDF", "e.docx"]
    assert listing.complete is False
    assert listing.truncated_reason.startswith("The listing stopped early:") and expected in listing.truncated_reason


def test_an_error_on_a_later_page_is_also_partial(fake):
    original = fake._children

    def second_page_fails(request):
        if request.url.params.get("$skiptoken") == "1":
            return graph_error(503, "serviceNotAvailable", "busy", **{"Retry-After": "0"})
        return original(request, CRM_ID)

    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{CRM_ID}/children", second_page_fails)
    listing = make_source(fake).list_files(target(), recursive=True, extensions=EXTS, max_files=100)
    assert [f.name for f in listing.files] == ["a.pdf", "B.PDF"] and listing.complete is False
    assert "throttling" in listing.truncated_reason


def test_an_error_before_anything_was_listed_raises(fake):
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{CRM_ID}/children",
                  lambda r: graph_error(403, "accessDenied", "Access denied"))
    failure = failure_of(lambda: make_source(fake).list_files(target(), recursive=True, extensions=EXTS, max_files=9))
    assert failure.code == "forbidden" and failure.suggested_action == ACTION_PERMISSIONS

    gone = failure_of(lambda: make_source(FakeMicrosoft()).list_files(target("ITEM-MISSING"), recursive=True,
                                                                    extensions=EXTS, max_files=9))
    assert gone.code == "not_found" and "folder" in gone.reason


def test_a_next_link_to_another_host_is_never_followed(fake):
    def foreign(request):
        return httpx.Response(200, json={"value": [ITEMS["ITEM-A"]],
                                         "@odata.nextLink": "https://evil.example/v1.0/steal?$skiptoken=1"})

    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{CRM_ID}/children", foreign)
    listing = make_source(fake).list_files(target(), recursive=False, extensions=EXTS, max_files=9)
    assert [f.name for f in listing.files] == ["a.pdf"] and listing.complete is False
    assert all(r.url.host != "evil.example" for r in fake.requests)


# ---- token errors ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "aadsts, code, action_fragment",
    [
        (7000215, "auth_invalid_secret", "Certificates & secrets"),
        (7000222, "auth_expired_secret", "Create a new client secret"),
        (700016, "auth_app_not_found", "Client ID"),
        (90002, "auth_tenant_not_found", "Directory (tenant) ID"),
        (900023, "auth_tenant_not_found", "Directory (tenant) ID"),
        (7000112, "auth_failed", "Enterprise applications"),
        (123456, "auth_failed", "Tenant ID, Client ID and Client Secret"),
    ],
)
def test_aadsts_errors_map_to_admin_actionable_failures(fake, aadsts, code, action_fragment):
    fake.token_handler = token_error(aadsts)
    source = make_source(fake)
    failure = failure_of(source.acquire_token)
    assert failure.code == code
    assert f"AADSTS{aadsts}" in failure.reason
    assert action_fragment in failure.suggested_action
    assert SECRET not in failure.reason and SECRET not in (failure.suggested_action or "")
    if code == "auth_invalid_secret":
        assert "SharePoint Sync page" in failure.suggested_action
    # a failed sign-in stops everything: no Graph request is made
    failure_of(lambda: source.resolve(SITE_URL, None, None))
    assert fake.graph_requests() == []


def test_unknown_aadsts_keeps_only_the_scrubbed_first_line(fake):
    fake.token_handler = token_error(123456)
    failure = failure_of(make_source(fake).acquire_token)
    assert failure.reason.startswith("Microsoft sign-in failed: AADSTS123456: Something failed for the secret ***")
    assert "Trace ID" not in failure.reason and "Correlation" not in failure.reason


def test_the_aadsts_code_is_read_from_the_description_too(fake):
    fake.token_handler = token_error(7000222, with_codes=False)
    assert failure_of(make_source(fake).acquire_token).code == "auth_expired_secret"


def test_token_endpoint_non_json_and_network_errors(fake):
    fake.token_handler = lambda r: httpx.Response(403, text="<html>blocked by proxy</html>")
    blocked = failure_of(make_source(fake).acquire_token)
    assert blocked.code == "auth_failed" and "proxy" in blocked.reason and blocked.suggested_action == ACTION_NETWORK

    def unreachable(request):
        raise httpx.ConnectError("getaddrinfo failed", request=request)

    fake.token_handler = unreachable
    down = failure_of(make_source(fake).acquire_token)
    assert down.code == "network" and down.suggested_action == ACTION_NETWORK
    assert "login.microsoftonline.com" in down.reason

    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    fake.token_handler = slow
    assert "Timed out" in failure_of(make_source(fake).acquire_token).reason

    fake.token_handler = lambda r: httpx.Response(200, json={"token_type": "Bearer"})
    assert failure_of(make_source(fake).acquire_token).code == "auth_failed"


def test_the_token_is_cached_until_five_minutes_before_expiry(fake):
    source = make_source(fake)
    now = [1000.0]
    source._auth._clock = lambda: now[0]
    source.acquire_token()
    source.acquire_token()
    now[0] += 3599 - 301  # still inside the window
    source.acquire_token()
    assert len(fake.token_requests()) == 1
    now[0] += 2  # within 5 minutes of expiry
    source.acquire_token()
    assert len(fake.token_requests()) == 2


def test_a_short_lived_token_is_cached_for_half_its_life(fake):
    fake.token_handler = lambda r: httpx.Response(200, json={"access_token": TOKEN, "expires_in": "400"})
    source = make_source(fake)
    now = [0.0]
    source._auth._clock = lambda: now[0]
    source.acquire_token()
    now[0] = 199.0
    source.acquire_token()
    assert len(fake.token_requests()) == 1
    now[0] = 201.0
    source.acquire_token()
    assert len(fake.token_requests()) == 2


def test_a_401_reauthenticates_once(fake):
    fake.tokens = ["stale-token", TOKEN]
    real = fake._graph

    def only_the_fresh_token(request, path):
        if request.headers["authorization"] != f"Bearer {TOKEN}":
            return graph_error(401, "InvalidAuthenticationToken", "Access token has expired")
        return real(request, path)

    fake._graph = only_the_fresh_token
    assert make_source(fake).resolve(SITE_URL, None, None) == target(ROOT_ID)
    assert len(fake.token_requests()) == 2


def test_a_second_401_is_auth_failed_with_the_permissions_action(fake):
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales",
                  lambda r: graph_error(401, "InvalidAuthenticationToken", "Either scp or roles claim need to be present"))
    failure = failure_of(lambda: make_source(fake).resolve(SITE_URL, None, None))
    assert (failure.code, failure.status_code) == ("auth_failed", 401)
    assert failure.suggested_action == ACTION_PERMISSIONS
    assert len(fake.token_requests()) == 2  # the library re-authenticated once


# ---- Graph errors -------------------------------------------------------------------------------


def test_403_is_forbidden_with_the_permission_hint(fake):
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales", lambda r: graph_error(403, "accessDenied", "Access denied"))
    failure = failure_of(lambda: make_source(fake).resolve(SITE_URL, None, None))
    assert (failure.code, failure.status_code) == ("forbidden", 403)
    assert failure.suggested_action == (
        "Grant Sites.Read.All + Files.Read.All (Application) and admin consent, "
        "or use Sites.Selected with a grant for this site"
    )
    assert "SharePoint site" in failure.reason


def test_429_after_the_library_retries_is_throttled(fake):
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales",
                  lambda r: graph_error(429, "activityLimitReached", "throttled", **{"Retry-After": "0"}))
    failure = failure_of(lambda: make_source(fake).resolve(SITE_URL, None, None))
    assert (failure.code, failure.status_code) == ("throttled", 429)
    assert len([r for r in fake.graph_requests() if "sites" in r.url.path]) == 4  # 1 + 3 retries


@pytest.mark.parametrize(
    "error, fragment",
    [(httpx.ConnectError("connection refused"), "Could not reach"), (httpx.ReadTimeout("slow"), "Timed out")],
)
def test_network_errors_on_graph(fake, error, fragment):
    def broken(request):
        error.request = request
        raise error

    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales", broken)
    failure = failure_of(lambda: make_source(fake).resolve(SITE_URL, None, None))
    assert failure.code == "network" and fragment in failure.reason
    assert failure.suggested_action == "Allow outbound HTTPS to login.microsoftonline.com and graph.microsoft.com"


# ---- download -----------------------------------------------------------------------------------


def remote_file(size: int | None = len(CONTENT), item_id: str = "ITEM-A") -> RemoteFile:
    return RemoteFile(drive_id=DRIVE_ID, item_id=item_id, name="a.pdf", path="/CRM", web_url=None, size=size,
                      etag=None, ctag=None, quick_xor_hash=None, modified_at=None, mime_type="application/pdf")


def test_download_streams_to_disk_and_returns_the_sha256(fake, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    dest = tmp_path / "original.pdf"
    digest = make_source(fake).download(remote_file(), dest, max_bytes=10 * 1024 * 1024)
    assert digest == hashlib.sha256(CONTENT).hexdigest()
    assert dest.read_bytes() == CONTENT
    assert not (tmp_path / "original.pdf.part").exists()
    content_request, download_request = fake.requests[-2:]
    assert content_request.url.path == f"/v1.0/drives/{DRIVE_ID}/items/ITEM-A/content"
    assert content_request.headers["authorization"] == f"Bearer {TOKEN}"
    assert download_request.url.host == HOST and "authorization" not in download_request.headers
    # httpx logs request URLs at INFO: the short-lived download token is redacted.
    assert "download.aspx" in caplog.text
    assert TEMPAUTH not in caplog.text and "tempauth=<redacted>" in caplog.text


def test_download_enforces_the_size_cap(fake, tmp_path):
    dest = tmp_path / "original.pdf"
    declared = failure_of(lambda: make_source(fake).download(remote_file(size=5000), dest, max_bytes=4096))
    assert declared.code == "too_large" and "MB" in declared.reason
    assert fake.requests == []  # refused before any request

    streamed = failure_of(lambda: make_source(fake).download(remote_file(size=None), dest, max_bytes=100))
    assert streamed.code == "too_large"
    assert not dest.exists() and not (tmp_path / "original.pdf.part").exists()


def test_download_errors(fake, tmp_path):
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/ITEM-X/content",
                  lambda r: graph_error(404, "itemNotFound", "gone"))
    gone = failure_of(lambda: make_source(fake).download(remote_file(item_id="ITEM-X"), tmp_path / "x.pdf",
                                                         max_bytes=10**7))
    assert gone.code == "not_found" and "no longer exists" in gone.reason

    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/ITEM-Y/content",
                  lambda r: graph_error(403, "accessDenied", "no"))
    denied = failure_of(lambda: make_source(fake).download(remote_file(item_id="ITEM-Y"), tmp_path / "y.pdf",
                                                           max_bytes=10**7))
    assert denied.code == "forbidden"

    missing_dir = tmp_path / "does-not-exist" / "z.pdf"
    storage = failure_of(lambda: make_source(fake).download(remote_file(), missing_dir, max_bytes=10**7))
    assert storage.code == "storage_error"


# ---- validate_site_url ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, normalized",
    [
        ("https://contoso.sharepoint.com/sites/Sales", "https://contoso.sharepoint.com/sites/Sales"),
        ("  HTTPS://Contoso.SharePoint.COM/sites/Sales/  ", "https://contoso.sharepoint.com/sites/Sales"),
        ("https://contoso.sharepoint.com/sites/Sales?web=1#top", "https://contoso.sharepoint.com/sites/Sales"),
        ("https://contoso.sharepoint.com/sites/Sales Team", "https://contoso.sharepoint.com/sites/Sales%20Team"),
        ("https://contoso.sharepoint.com/sites/Sales%20Team", "https://contoso.sharepoint.com/sites/Sales%20Team"),
        ("https://contoso.sharepoint.com", "https://contoso.sharepoint.com"),
        ("https://contoso.sharepoint.com.", "https://contoso.sharepoint.com"),
        ("https://contoso-my.sharepoint.com/personal/adele_contoso_com",
         "https://contoso-my.sharepoint.com/personal/adele_contoso_com"),
    ],
)
def test_validate_site_url_accepts_and_normalizes(url, normalized):
    assert validate_site_url(url, make_settings()) == normalized


@pytest.mark.parametrize(
    "url",
    [
        "", "   ", "http://contoso.sharepoint.com/sites/Sales", "ftp://contoso.sharepoint.com/sites/Sales",
        "contoso.sharepoint.com/sites/Sales",
        "https://evil.example/sites/Sales", "https://evilsharepoint.com/sites/Sales",
        "https://contoso.sharepoint.com.evil.example/sites/Sales", "https://sharepoint.com/sites/Sales",
        "https://user:pass@contoso.sharepoint.com/sites/Sales", "https://user@contoso.sharepoint.com/sites/Sales",
        "https://@contoso.sharepoint.com/sites/Sales",
        "https://contoso.sharepoint.com:443/sites/Sales", "https://contoso.sharepoint.com:8443/sites/Sales",
        "https://contoso.sharepoint.com:/sites/Sales", "https://contoso.sharepoint.com:abc/sites/Sales",
        "https://127.0.0.1/sites/Sales", "https://[::1]/sites/Sales", "https://169.254.169.254/latest",
        "https://contoso.sharepoint.com\\@evil.example/", "https://contoso.sharepoint.com/sites/Sa\nles",
        "https://contoso.sharepoint.com/:f:/s/Sales/EabcDEF?e=1",
        "https://contoso.sharepoint.com/sites/Sales/SitePages/Home.aspx",
        "https://contoso.sharepoint.com/sites/Sales/Shared%20Documents/Forms/AllItems.aspx",
        "https://contoso.sharepoint.com/sites/Sales/_layouts/15/viewlsts.aspx",
        "https://contoso.sharepoint.com/sites/../admin", "https://contoso.sharepoint.com/sites/a%2Fb",
        "https://" + "a" * 1000 + ".sharepoint.com/",
    ],
)
def test_validate_site_url_rejects(url):
    failure = failure_of(lambda: validate_site_url(url, make_settings()))
    assert failure.code == "invalid_config" and failure.reason


def test_validate_site_url_suffix_configuration():
    us = make_settings(sharepoint_host_suffixes=".sharepoint.us, sharepoint.com")  # the 2nd has no dot
    assert validate_site_url("https://agency.sharepoint.us/sites/x", us) == "https://agency.sharepoint.us/sites/x"
    assert validate_site_url("https://contoso.sharepoint.com", us) == "https://contoso.sharepoint.com"
    failure_of(lambda: validate_site_url("https://evilsharepoint.com/", us))  # "sharepoint.com" means ".sharepoint.com"
    none = failure_of(lambda: validate_site_url(SITE_URL, make_settings(sharepoint_host_suffixes=" , ")))
    assert none.code == "invalid_config" and "DOC_INTEL_SHAREPOINT_HOST_SUFFIXES" in none.suggested_action


# ---- configuration and credentials ---------------------------------------------------------------


def test_the_graph_settings_never_fall_back_to_the_environment(fake, monkeypatch):
    monkeypatch.setenv("MICROSOFT_GRAPH_BASE_URL", "https://evil.example/v1.0")
    monkeypatch.setenv("MICROSOFT_AUTHORITY_HOST", "https://evil.example")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "env-secret-must-not-be-used")
    monkeypatch.setenv("MICROSOFT_MAX_DOWNLOAD_BYTES", "1")
    with make_source(fake) as source:
        assert source.resolve(SITE_URL, None, None) == target(ROOT_ID)
        assert source._client().settings.graph_base_url == "https://graph.microsoft.com/v1.0"
        assert source._client().settings.max_download_bytes == 100 * 1024 * 1024
    assert {r.url.host for r in fake.requests} == {"login.microsoftonline.com", "graph.microsoft.com"}
    assert b"env-secret-must-not-be-used" not in fake.token_requests()[0].content


@pytest.mark.parametrize("field", ["graph_authority_host", "graph_base_url"])
def test_the_microsoft_endpoints_must_be_https(fake, field):
    settings = make_settings(**{field: "http://login.microsoftonline.com"})
    failure = failure_of(make_source(fake, settings=settings).acquire_token)
    assert failure.code == "invalid_config" and "https" in failure.reason
    assert fake.requests == []


@pytest.mark.parametrize(
    "tenant, client, secret, fragment",
    [
        ("", CLIENT, SECRET, "Tenant ID is missing"),
        (TENANT, "", "", "Client ID and Client Secret are missing"),
        ("contoso/../x", CLIENT, SECRET, "Tenant ID"),
        ("common", CLIENT, SECRET, "Tenant ID"),
        (TENANT, "my-app", SECRET, "Client ID"),
        (TENANT, CLIENT, "bad\nsecret", "control characters"),
    ],
)
def test_credentials_are_checked_before_any_request(fake, tenant, client, secret, fragment):
    failure = failure_of(make_source(fake, tenant=tenant, client=client, secret=secret).acquire_token)
    assert failure.code == "invalid_config" and fragment in failure.reason
    assert fake.requests == []


def test_a_tenant_domain_is_accepted_and_whitespace_trimmed(fake):
    fake.override("POST", "/contoso.onmicrosoft.com/oauth2/v2.0/token",
                  lambda r: httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3599}))
    make_source(fake, tenant=" contoso.onmicrosoft.com ", secret=f" {SECRET}\n").acquire_token()
    form = parse_qs(fake.requests[0].content.decode())
    assert form["client_secret"] == [SECRET]


def test_without_the_library_every_call_reports_unavailable(fake, monkeypatch):
    monkeypatch.setattr(graph_source, "_LIBRARY_ERROR", "crm-document-ingestion is not installed (test)")
    assert graph_available() == (False, "crm-document-ingestion is not installed (test)")
    source = make_source(fake)
    assert failure_of(source.acquire_token).code == "unavailable"
    result = source.test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)
    assert result["ok"] is False and result["steps"][0]["key"] == "credentials"
    assert "not installed" in result["steps"][0]["detail"]
    assert fake.requests == []


def test_graph_available_in_this_environment():
    assert graph_available() == (True, None)


# ---- test_connection ------------------------------------------------------------------------------


def test_connection_test_success(fake):
    result = make_source(fake).test_connection(SITE_URL, "Documents", "/CRM", recursive=True, extensions=EXTS)
    assert result["ok"] is True
    assert [s["key"] for s in result["steps"]] == ["credentials", "token", "site", "drive", "folder", "listing"]
    assert all(s["ok"] and isinstance(s["latency_ms"], int) and s["detail"] for s in result["steps"])
    assert all(set(s) == {"key", "label", "ok", "latency_ms", "detail", "suggested_action"} for s in result["steps"])
    assert result["files_found"] == 5
    assert result["sample_files"] == ["a.pdf", "B.PDF", "e.docx", "c.docx", "d.pdf"]
    assert "5 matching PDF/DOCX found (including subfolders)" in result["steps"][-1]["detail"]
    assert result["checked_at"].endswith("Z")
    json.dumps(result)  # plain JSON for ConnectionTestOut


def test_connection_test_stops_at_the_failed_step(fake):
    fake.token_handler = token_error(7000215)
    result = make_source(fake).test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)
    assert result["ok"] is False
    assert [(s["key"], s["ok"]) for s in result["steps"]] == [("credentials", True), ("token", False)]
    token = result["steps"][1]
    assert "AADSTS7000215" in token["detail"] and "Certificates & secrets" in token["suggested_action"]
    assert result["files_found"] is None and result["sample_files"] == []


def test_connection_test_with_missing_credentials_makes_no_request(fake):
    result = make_source(fake, secret="").test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)
    assert [(s["key"], s["ok"]) for s in result["steps"]] == [("credentials", False)]
    assert "Client Secret is missing" in result["steps"][0]["detail"]
    assert fake.requests == []


def test_connection_test_reports_a_missing_folder(fake):
    result = make_source(fake).test_connection(SITE_URL, None, "/Nope", recursive=True, extensions=EXTS)
    assert [(s["key"], s["ok"]) for s in result["steps"]] == [
        ("credentials", True), ("token", True), ("site", True), ("drive", True), ("folder", False)]
    assert "'/Nope'" in result["steps"][-1]["detail"]


def test_connection_test_reports_a_bad_site_url_at_the_site_step(fake):
    result = make_source(fake).test_connection("https://evil.example/", None, None, recursive=True, extensions=EXTS)
    assert [(s["key"], s["ok"]) for s in result["steps"]][-1] == ("site", False)


def test_connection_test_listing_counts_are_capped(fake, monkeypatch):
    monkeypatch.setattr(graph_source, "_TEST_MAX_FILES", 2)
    result = make_source(fake).test_connection(SITE_URL, None, "/CRM", recursive=True, extensions=EXTS)
    listing = result["steps"][-1]
    assert result["ok"] is True and listing["ok"] is True and listing["detail"].startswith("At least 2")
    assert result["files_found"] == 2


def test_connection_test_listing_error_fails_the_listing_step(fake):
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{SUB_ID}/children",
                  lambda r: graph_error(403, "accessDenied", "Access denied"))
    result = make_source(fake).test_connection(SITE_URL, None, "/CRM", recursive=True, extensions=EXTS)
    listing = result["steps"][-1]
    assert (listing["key"], listing["ok"], result["ok"]) == ("listing", False, False)
    assert listing["suggested_action"] == ACTION_PERMISSIONS
    assert result["files_found"] == 3


def test_connection_test_stops_waiting_for_long_throttling(fake):
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales",
                  lambda r: graph_error(429, "activityLimitReached", "throttled", **{"Retry-After": "59"}))
    result = make_source(fake).test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)
    site = result["steps"][-1]
    assert (site["key"], site["ok"]) == ("site", False) and "throttling" in site["detail"]


def test_connection_test_never_raises(fake):
    def explode(request):
        raise RuntimeError("unexpected bug in a transport")

    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales", explode)
    result = make_source(fake).test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)
    assert result["ok"] is False
    assert result["steps"][-1]["key"] == "unexpected" and "RuntimeError" in result["steps"][-1]["detail"]


def test_failed_connection_test_helper():
    result = failed_connection_test("The stored credentials cannot be decrypted", "Set DOC_INTEL_SECRETS_KEY")
    assert result["ok"] is False and result["files_found"] is None and result["checked_at"].endswith("Z")
    assert result["steps"] == [{"key": "credentials", "label": "Credentials", "ok": False, "latency_ms": None,
                                "detail": "The stored credentials cannot be decrypted",
                                "suggested_action": "Set DOC_INTEL_SECRETS_KEY"}]


# ---- secrets never leak ---------------------------------------------------------------------------


def test_the_secret_and_tokens_never_appear_in_exceptions_results_or_logs(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    def record(exc: GraphFailed) -> None:
        seen.extend([str(exc), repr(exc), exc.reason, exc.suggested_action or "", repr(exc.args)])

    # 1) every token failure, with Microsoft echoing the secret in its description
    for aadsts in (7000215, 7000222, 700016, 90002, 900023, 123456):
        fake = FakeMicrosoft()
        fake.token_handler = token_error(aadsts)
        source = make_source(fake)
        with pytest.raises(GraphFailed) as info:
            source.acquire_token()
        record(info.value)
        seen.append(json.dumps(source.test_connection(SITE_URL, None, None, recursive=True, extensions=EXTS)))

    # 2) Graph errors whose messages echo the secret and the bearer token
    echo = f"bad request: secret={SECRET} header=Bearer {TOKEN} jwt=eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0.sig"
    fake = FakeMicrosoft()
    fake.override("GET", f"/v1.0/sites/{HOST}:/sites/Sales", lambda r: graph_error(400, "invalidRequest", echo))
    fake.override("GET", f"/v1.0/drives/{DRIVE_ID}/items/{CRM_ID}/children", lambda r: graph_error(418, echo, echo))
    source = make_source(fake)
    with pytest.raises(GraphFailed) as info:
        source.resolve(SITE_URL, None, None)
    record(info.value)
    assert "***" in info.value.reason and "<token>" in info.value.reason
    with pytest.raises(GraphFailed) as info:
        source.list_files(target(), recursive=True, extensions=EXTS, max_files=10)
    record(info.value)
    seen.append(json.dumps(source.test_connection(SITE_URL, None, "/CRM", recursive=True, extensions=EXTS)))

    # 3) a successful run, a download and the reprs
    fake = FakeMicrosoft()
    source = make_source(fake)
    seen.append(json.dumps(source.test_connection(SITE_URL, None, "/CRM", recursive=True, extensions=EXTS)))
    source.download(remote_file(), tmp_path / "a.pdf", max_bytes=10**7)
    seen.extend([repr(source), repr(source._credentials), repr(source._auth)])

    haystack = "\n".join(seen) + "\n" + caplog.text
    assert SECRET not in haystack
    assert TOKEN not in haystack
    assert TEMPAUTH not in haystack
