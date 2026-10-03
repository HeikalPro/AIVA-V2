"""Site and document-library resolution, paths, paged folder listing and streamed downloads
(SharePointClient). Graph is faked with httpx.MockTransport; no network."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from crm_ingestion.connectors.sharepoint import (
    drive_from_graph,
    drive_item_from_graph,
    site_from_graph,
)
from crm_ingestion.errors import (
    DownloadError,
    DownloadLimitError,
    DriveNotFoundError,
    GraphAPIError,
    GraphTransportError,
    ItemNotFoundError,
)

from .fakes import (
    GRAPH,
    SP_DRIVE_ID,
    SP_SITE_ID,
    folder_item,
    graph_error,
    graph_settings,
    make_client,
    sharepoint_item,
)

HOST = "contoso.sharepoint.com"
FOLDER_ID = "01ABCDEFFOLDERCRM"
ARABIC = "عقود"


def site_json(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": SP_SITE_ID,
        "name": "Sales",
        "displayName": "Sales Team",
        "webUrl": f"https://{HOST}/sites/Sales",
        "siteCollection": {"hostname": HOST},
    }
    data.update(overrides)
    return data


def drive_json(
    drive_id: str = SP_DRIVE_ID, name: str = "Documents", leaf: str = "Shared%20Documents"
) -> dict[str, Any]:
    return {
        "id": drive_id,
        "name": name,
        "driveType": "documentLibrary",
        "webUrl": f"https://{HOST}/sites/Sales/{leaf}",
        "description": "",
    }


def child(name: str, *, folder: bool = False, **overrides: Any) -> dict[str, Any]:
    """A driveItem as it appears in a children listing of the /CRM folder."""
    data: dict[str, Any] = {
        "id": f"id-{name}",
        "name": name,
        "size": 10,
        "eTag": f'"{{E-{name}}},1"',
        "cTag": f'"c:{{E-{name}}},2"',
        "lastModifiedDateTime": "2026-09-20T14:30:45Z",
        "webUrl": f"https://{HOST}/sites/Sales/Shared%20Documents/CRM/{name}",
        "parentReference": {
            "driveId": SP_DRIVE_ID,
            "id": FOLDER_ID,
            "path": f"/drives/{SP_DRIVE_ID}/root:/CRM",
        },
    }
    if folder:
        data["folder"] = {"childCount": 2}
    else:
        data["file"] = {"mimeType": "application/pdf", "hashes": {"quickXorHash": f"qx-{name}"}}
    data.update(overrides)
    return data


def raw_path(request: httpx.Request) -> str:
    return request.url.raw_path.decode("ascii").split("?", 1)[0]


# ---- sites ---------------------------------------------------------------------------


def test_resolve_site_by_server_relative_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=site_json())

    site = make_client(handler).resolve_site(f"https://{HOST}/sites/Sales")
    assert raw_path(seen[0]) == f"/v1.0/sites/{HOST}:/sites/Sales"
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert (site.id, site.name, site.display_name, site.hostname) == (SP_SITE_ID, "Sales", "Sales Team", HOST)
    assert site.web_url == f"https://{HOST}/sites/Sales"
    assert site.raw["siteCollection"] == {"hostname": HOST}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}", f"/v1.0/sites/{HOST}"),
        (f"https://{HOST}/", f"/v1.0/sites/{HOST}"),
        (f"https://{HOST.upper()}/sites/Sales%20Team/", f"/v1.0/sites/{HOST}:/sites/Sales%20Team"),
        (f"https://{HOST}/teams/{ARABIC}", f"/v1.0/sites/{HOST}:/teams/%D8%B9%D9%82%D9%88%D8%AF"),
        (f"https://{HOST}/sites/a%2Fb?web=1#top", f"/v1.0/sites/{HOST}:/sites/a%2Fb"),
    ],
)
def test_resolve_site_root_and_segment_encoding(url: str, expected: str) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(raw_path(request))
        return httpx.Response(200, json=site_json())

    make_client(handler).resolve_site(url)
    assert seen == [expected]


@pytest.mark.parametrize("url", [f"http://{HOST}/sites/Sales", f"{HOST}/sites/Sales", "https:///sites/x", ""])
def test_resolve_site_needs_an_https_url(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(ValueError, match="https"):
        make_client(handler).resolve_site(url)


def test_resolve_site_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(404, "itemNotFound", "Requested site could not be found")

    with pytest.raises(ItemNotFoundError) as info:
        make_client(handler).resolve_site(f"https://{HOST}/sites/Nope")
    assert info.value.status_code == 404


def test_site_and_drive_mapping_require_an_id() -> None:
    with pytest.raises(GraphAPIError):
        site_from_graph({"name": "x"})
    with pytest.raises(GraphAPIError):
        drive_from_graph({"name": "x"})
    drive = drive_from_graph({"id": "d1"}, site_id="s1")
    assert (drive.id, drive.name, drive.site_id, drive.description) == ("d1", "d1", "s1", None)


# ---- document libraries --------------------------------------------------------------


def test_list_drives_follows_next_link() -> None:
    next_link = f"{GRAPH}/sites/{SP_SITE_ID}/drives?$skiptoken=page2"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "value": [drive_json(), drive_json("d2", "Contracts", "Contracts")],
                    "@odata.nextLink": next_link,
                },
            )
        return httpx.Response(200, json={"value": [drive_json("d3", "Site Assets", "SiteAssets"), "junk"]})

    drives = make_client(handler).list_drives(SP_SITE_ID)
    assert [d.name for d in drives] == ["Documents", "Contracts", "Site Assets"]
    assert all(d.site_id == SP_SITE_ID for d in drives)
    assert seen[0].url.path == f"/v1.0/sites/{SP_SITE_ID}/drives"
    assert str(seen[1].url) == next_link
    assert all(r.headers["Authorization"] == "Bearer test-token" for r in seen)


def test_get_drive_default_library() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=drive_json())

    for name in (None, "", "   "):
        drive = make_client(handler).get_drive(SP_SITE_ID, name)
        assert (drive.id, drive.name, drive.drive_type) == (SP_DRIVE_ID, "Documents", "documentLibrary")
    assert {r.url.path for r in seen} == {f"/v1.0/sites/{SP_SITE_ID}/drive"}


def test_get_drive_by_name_or_url_segment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1.0/sites/{SP_SITE_ID}/drives"
        return httpx.Response(
            200, json={"value": [drive_json(), drive_json("d2", "Client Contracts", "Contracts")]}
        )

    client = make_client(handler)
    assert client.get_drive(SP_SITE_ID, "documents").id == SP_DRIVE_ID
    assert client.get_drive(SP_SITE_ID, " Shared Documents ").id == SP_DRIVE_ID  # the URL segment
    assert client.get_drive(SP_SITE_ID, "CLIENT CONTRACTS").id == "d2"
    assert client.get_drive(SP_SITE_ID, "contracts").id == "d2"
    with pytest.raises(ItemNotFoundError) as info:
        client.get_drive(SP_SITE_ID, "Invoices")
    assert isinstance(info.value, DriveNotFoundError)
    assert (info.value.status_code, info.value.code) == (404, "driveNotFound")
    assert (info.value.name, info.value.available) == ("Invoices", ["Documents", "Client Contracts"])
    assert "Documents, Client Contracts" in str(info.value)


# ---- items by path -------------------------------------------------------------------


@pytest.mark.parametrize("path", [None, "", "/", "//"])
def test_get_item_by_path_root(path: str | None) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(raw_path(request))
        # a drive's root carries no parentReference.driveId here: the requested drive is used
        return httpx.Response(
            200, json={"id": "root-id", "name": "root", "root": {}, "folder": {"childCount": 4}}
        )

    item = make_client(handler).get_item_by_path(SP_DRIVE_ID, path)
    assert seen == [f"/v1.0/drives/{SP_DRIVE_ID}/root"]
    assert (item.id, item.drive_id, item.is_root, item.is_folder, item.is_file) == (
        "root-id",
        SP_DRIVE_ID,
        True,
        True,
        False,
    )
    assert item.child_count == 4


def test_get_item_by_path_encodes_every_segment() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(raw_path(request))
        return httpx.Response(200, json=child("Q3", folder=True))

    client = make_client(handler)
    client.get_item_by_path(SP_DRIVE_ID, f"/CRM/Q3 Contracts/{ARABIC}/")
    client.get_item_by_path(SP_DRIVE_ID, "CRM/100% sure #1")
    assert seen == [
        f"/v1.0/drives/{SP_DRIVE_ID}/root:/CRM/Q3%20Contracts/%D8%B9%D9%82%D9%88%D8%AF",
        f"/v1.0/drives/{SP_DRIVE_ID}/root:/CRM/100%25%20sure%20%231",
    ]


def test_get_item_by_path_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(404, "itemNotFound", "The resource could not be found.")

    with pytest.raises(ItemNotFoundError):
        make_client(handler).get_item_by_path(SP_DRIVE_ID, "/missing")


# ---- folder listing ------------------------------------------------------------------


def test_iter_children_follows_next_link_and_maps_facets() -> None:
    next_link = f"{GRAPH}/drives/{SP_DRIVE_ID}/items/{FOLDER_ID}/children?$top=200&$skiptoken=abc"
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            page = [child("a.pdf"), child("Sub", folder=True)]
            return httpx.Response(200, json={"value": page, "@odata.nextLink": next_link})
        return httpx.Response(
            200, json={"value": [child("b.docx"), child("gone.pdf", deleted={"state": "deleted"})]}
        )

    items = list(make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID))

    assert seen[0].url.path == f"/v1.0/drives/{SP_DRIVE_ID}/items/{FOLDER_ID}/children"
    assert seen[0].url.params["$top"] == "200"
    assert str(seen[1].url) == next_link
    assert all(r.headers["Authorization"] == "Bearer test-token" for r in seen)
    assert [i.name for i in items] == ["a.pdf", "Sub", "b.docx", "gone.pdf"]
    pdf, sub, _, gone = items
    assert (pdf.is_file, pdf.is_folder, pdf.is_deleted) == (True, False, False)
    assert pdf.ctag == '"c:{E-a.pdf},2"' and pdf.etag == '"{E-a.pdf},1"'
    assert pdf.quick_xor_hash == "qx-a.pdf" and pdf.mime_type == "application/pdf"
    assert pdf.parent_id == FOLDER_ID and pdf.drive_id == SP_DRIVE_ID
    assert pdf.modified_at is not None and pdf.modified_at.isoformat() == "2026-09-20T14:30:45+00:00"
    assert (sub.is_file, sub.is_folder, sub.child_count) == (False, True, 2)
    assert gone.is_deleted is True


def test_iter_children_page_size() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"value": []})

    client = make_client(handler)
    assert list(client.iter_children(SP_DRIVE_ID, FOLDER_ID, page_size=50)) == []
    assert seen[0].url.params["$top"] == "50"
    for bad in (0, 1000):
        with pytest.raises(ValueError, match="page_size"):
            list(client.iter_children(SP_DRIVE_ID, FOLDER_ID, page_size=bad))


def test_iter_children_retries_a_throttled_page() -> None:
    next_link = f"{GRAPH}/drives/{SP_DRIVE_ID}/items/{FOLDER_ID}/children?$skiptoken=p2"
    responses = [
        httpx.Response(200, json={"value": [child("a.pdf")], "@odata.nextLink": next_link}),
        graph_error(429, "activityLimitReached", "throttled", **{"Retry-After": "3"}),
        graph_error(503, "serviceNotAvailable", "busy", **{"Retry-After": "1"}),
        httpx.Response(200, json={"value": [child("b.pdf")]}),
    ]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    names = [i.name for i in make_client(handler, sleeps=sleeps).iter_children(SP_DRIVE_ID, FOLDER_ID)]
    assert names == ["a.pdf", "b.pdf"]
    assert sleeps == [3.0, 1.0]


def test_iter_children_error_on_a_later_page_comes_after_the_first_page() -> None:
    next_link = f"{GRAPH}/drives/{SP_DRIVE_ID}/items/{FOLDER_ID}/children?$skiptoken=p2"

    def handler(request: httpx.Request) -> httpx.Response:
        if "skiptoken" in str(request.url):
            return graph_error(500, "generalException", "boom")
        return httpx.Response(
            200, json={"value": [child("a.pdf"), child("b.pdf")], "@odata.nextLink": next_link}
        )

    received: list[str] = []
    with pytest.raises(GraphAPIError) as info:
        for item in make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID):
            received.append(item.name)
    assert received == ["a.pdf", "b.pdf"]
    assert info.value.status_code == 500


def test_iter_children_refuses_a_next_link_to_another_host() -> None:
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(
            200,
            json={
                "value": [child("a.pdf")],
                "@odata.nextLink": "https://evil.example/v1.0/steal?$skiptoken=x",
            },
        )

    received: list[str] = []
    with pytest.raises(GraphAPIError, match="another host"):
        for item in make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID):
            received.append(item.name)
    assert received == ["a.pdf"]
    assert hosts == ["graph.microsoft.com"]  # the bearer token never went to evil.example


def test_iter_children_refuses_a_next_link_with_another_scheme_or_port() -> None:
    for link in ("http://graph.microsoft.com/v1.0/x", "https://graph.microsoft.com:8443/v1.0/x"):

        def handler(request: httpx.Request, link: str = link) -> httpx.Response:
            return httpx.Response(200, json={"value": [], "@odata.nextLink": link})

        with pytest.raises(GraphAPIError, match="another host"):
            list(make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID))


def test_iter_children_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    with pytest.raises(GraphTransportError) as info:
        list(make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID))
    assert info.value.status_code == 0


def test_iter_children_non_json_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy login</html>")

    with pytest.raises(GraphAPIError, match="not valid JSON"):
        list(make_client(handler).iter_children(SP_DRIVE_ID, FOLDER_ID))


# ---- facets on existing items ---------------------------------------------------------


def test_new_facets_leave_existing_items_unchanged() -> None:
    item = drive_item_from_graph(sharepoint_item())
    assert (item.is_file, item.is_folder, item.is_root, item.is_deleted, item.child_count) == (
        True,
        False,
        False,
        False,
        None,
    )
    assert item.parent_id == "01ABCDEFPARENTFOLDERID"
    folder = drive_item_from_graph(folder_item())
    assert (folder.is_file, folder.is_folder, folder.child_count) == (False, True, 3)


def test_drive_id_fallback_only_when_the_response_has_none() -> None:
    bare = {"id": "x", "name": "y", "file": {}}
    assert drive_item_from_graph(bare, drive_id="fallback").drive_id == "fallback"
    assert drive_item_from_graph(sharepoint_item(), drive_id="fallback").drive_id == SP_DRIVE_ID
    with pytest.raises(GraphAPIError):
        drive_item_from_graph(bare)


# ---- streamed download ---------------------------------------------------------------


def test_download_to_streams_chunks_through_the_content_redirect() -> None:
    item = drive_item_from_graph(child("a.pdf", size=None))
    seen: list[httpx.Request] = []
    location = f"https://{HOST}/sites/Sales/_layouts/15/download.aspx?UniqueId=abc&tempauth=t0k"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "graph.microsoft.com":
            assert request.url.path == f"/v1.0/drives/{SP_DRIVE_ID}/items/id-a.pdf/content"
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, content=iter([b"%PDF-", b"1.7 ", b"body"]))

    chunks: list[bytes] = []
    written = make_client(handler).download_to(item, chunks.append)
    assert written == 13 and b"".join(chunks) == b"%PDF-1.7 body"
    assert [r.url.host for r in seen] == ["graph.microsoft.com", HOST]
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert "Authorization" not in seen[1].headers


def test_download_to_limits() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    declared = drive_item_from_graph(child("big.pdf", size=11))
    with pytest.raises(DownloadLimitError) as info:
        make_client(refuse).download_to(declared, lambda chunk: None, max_bytes=10)
    assert info.value.limit == 10 and isinstance(info.value, DownloadError)

    unknown = drive_item_from_graph(child("u.pdf", size=None))

    def with_length(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    with pytest.raises(DownloadLimitError, match="50 bytes"):
        make_client(with_length).download_to(unknown, lambda chunk: None, max_bytes=10)

    def streamed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([b"x" * 6, b"x" * 6]))  # no Content-Length

    written: list[bytes] = []
    with pytest.raises(DownloadLimitError, match="while downloading"):
        make_client(streamed).download_to(unknown, written.append, max_bytes=10)
    assert written == [b"x" * 6]  # stopped before the chunk that crossed the limit

    settings_limit = make_client(streamed, settings=graph_settings(max_download_bytes=5))
    with pytest.raises(DownloadLimitError):
        settings_limit.download_to(unknown, lambda chunk: None)
    with pytest.raises(ValueError):
        make_client(refuse).download_to(unknown, lambda chunk: None, max_bytes=0)


def test_download_to_errors() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(DownloadError, match="folder"):
        make_client(refuse).download_to(drive_item_from_graph(child("Sub", folder=True)), lambda chunk: None)

    def missing(request: httpx.Request) -> httpx.Response:
        return graph_error(404, "itemNotFound", "gone")

    with pytest.raises(DownloadError) as info:
        make_client(missing).download_to(drive_item_from_graph(child("a.pdf")), lambda chunk: None)
    assert not isinstance(info.value, DownloadLimitError)
    assert isinstance(info.value.__cause__, ItemNotFoundError)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DownloadError, match="network error") as net:
        make_client(broken).download_to(drive_item_from_graph(child("a.pdf")), lambda chunk: None)
    assert isinstance(net.value.__cause__, httpx.TransportError)
