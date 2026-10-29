from __future__ import annotations

import httpx
import pytest

from crm_ingestion.connectors.sharepoint import (
    AuthenticationProvider,
    SharePointClient,
    drive_item_from_graph,
)
from crm_ingestion.errors import DownloadError, GraphAPIError, ItemNotFoundError

from .fakes import (
    GRAPH,
    OD_DRIVE_ID,
    OD_ITEM_ID,
    SP_DRIVE_ID,
    SP_ITEM_ID,
    folder_item,
    graph_error,
    graph_settings,
    make_client,
    onedrive_item,
    sharepoint_item,
)


def test_get_drive_item_by_ids_sends_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=sharepoint_item())

    with make_client(handler) as client:
        item = client.get_drive_item(SP_DRIVE_ID, SP_ITEM_ID)
    assert item.id == SP_ITEM_ID
    assert str(seen[0].url) == f"{GRAPH}/drives/{SP_DRIVE_ID}/items/{SP_ITEM_ID}"
    assert seen[0].headers["Authorization"] == "Bearer test-token"


def test_item_id_with_bang_is_not_escaped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode() == f"/v1.0/drives/{OD_DRIVE_ID}/items/{OD_ITEM_ID}"
        return httpx.Response(200, json=onedrive_item())

    assert make_client(handler).get_drive_item(OD_DRIVE_ID, OD_ITEM_ID).id == OD_ITEM_ID


def test_resolve_sharing_url_hits_shares_endpoint() -> None:
    url = "https://contoso.sharepoint.com/:b:/s/Sales/EabcDEF?e=1a2b3c"
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=sharepoint_item())

    from crm_ingestion.connectors.sharepoint import DriveItemReference, encode_sharing_url

    item = make_client(handler).get_item(DriveItemReference.from_sharing_url(url))
    assert item.id == SP_ITEM_ID
    assert paths == [f"/v1.0/shares/{encode_sharing_url(url)}/driveItem"]
    assert paths[0].startswith("/v1.0/shares/u!")


def test_custom_base_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("https://graph.example.test/beta/drives/")
        return httpx.Response(200, json=onedrive_item())

    settings = graph_settings(graph_base_url="https://graph.example.test/beta/")
    make_client(handler, settings=settings).get_drive_item("d", "i")


def test_404_raises_item_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(404, "itemNotFound", "The resource could not be found.")

    with pytest.raises(ItemNotFoundError) as info:
        make_client(handler).get_drive_item("d", "missing")
    assert info.value.status_code == 404
    assert info.value.code == "itemNotFound"
    assert "could not be found" in str(info.value)


def test_403_raises_graph_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(403, "accessDenied", "Access denied")

    with pytest.raises(GraphAPIError) as info:
        make_client(handler).get_drive_item("d", "i")
    assert not isinstance(info.value, ItemNotFoundError)
    assert (info.value.status_code, info.value.code) == (403, "accessDenied")


def test_non_json_error_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    with pytest.raises(GraphAPIError, match="upstream exploded"):
        make_client(handler).get_drive_item("d", "i")


class CountingAuth(AuthenticationProvider):
    def __init__(self) -> None:
        self.generation = 0
        self.invalidations = 0

    def get_access_token(self) -> str:
        return f"token-{self.generation}"

    def invalidate(self) -> None:
        self.invalidations += 1
        self.generation += 1


def _client_with_auth(auth: AuthenticationProvider, handler: object) -> SharePointClient:
    return SharePointClient(
        auth,
        graph_settings(),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        sleep=lambda s: None,
    )


def test_401_invalidates_and_retries_once() -> None:
    auth = CountingAuth()
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer token-0":
            return graph_error(401, "InvalidAuthenticationToken", "Access token has expired")
        return httpx.Response(200, json=sharepoint_item())

    item = _client_with_auth(auth, handler).get_drive_item("d", "i")
    assert item.id == SP_ITEM_ID
    assert tokens == ["Bearer token-0", "Bearer token-1"]
    assert auth.invalidations == 1


def test_401_twice_raises() -> None:
    auth = CountingAuth()
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return graph_error(401, "InvalidAuthenticationToken", "nope")

    with pytest.raises(GraphAPIError) as info:
        _client_with_auth(auth, handler).get_drive_item("d", "i")
    assert info.value.status_code == 401
    assert len(calls) == 2


def test_429_honours_retry_after_with_injected_sleep() -> None:
    responses = [
        graph_error(429, "activityLimitReached", "throttled", **{"Retry-After": "7"}),
        graph_error(503, "serviceNotAvailable", "busy", **{"Retry-After": "2"}),
        httpx.Response(200, json=sharepoint_item()),
    ]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    item = make_client(handler, sleeps=sleeps).get_drive_item("d", "i")
    assert item.id == SP_ITEM_ID
    assert sleeps == [7.0, 2.0]


def test_429_gives_up_after_max_retries() -> None:
    sleeps: list[float] = []
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return graph_error(429, "activityLimitReached", "throttled")  # no Retry-After -> backoff

    with pytest.raises(GraphAPIError) as info:
        make_client(handler, sleeps=sleeps).get_drive_item("d", "i")
    assert info.value.status_code == 429
    assert len(calls) == 4
    assert sleeps == [1.0, 2.0, 4.0]


def test_transport_error_on_metadata_is_graph_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    with pytest.raises(GraphAPIError, match="network error"):
        make_client(handler).get_drive_item("d", "i")


# --- download ------------------------------------------------------------------------


def test_download_uses_preauthenticated_url_without_bearer() -> None:
    item = drive_item_from_graph(sharepoint_item())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-1.7 abc")

    assert make_client(handler).download_content(item) == b"%PDF-1.7 abc"
    assert seen[0].url.host == "contoso.sharepoint.com"
    assert "Authorization" not in seen[0].headers


def test_download_content_endpoint_redirect_drops_bearer() -> None:
    item = drive_item_from_graph(onedrive_item())
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "graph.microsoft.com":
            assert request.url.path == f"/v1.0/drives/{OD_DRIVE_ID}/items/{OD_ITEM_ID}/content"
            return httpx.Response(
                302, headers={"Location": "https://public.dm.files.1drv.com/y4m/notes.docx"}
            )
        return httpx.Response(200, content=b"hello")

    assert make_client(handler).download_content(item) == b"hello"
    assert [r.url.host for r in seen] == ["graph.microsoft.com", "public.dm.files.1drv.com"]
    assert seen[0].headers["Authorization"] == "Bearer test-token"
    assert "Authorization" not in seen[1].headers  # httpx strips it on cross-origin redirects


def test_download_rejects_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    with pytest.raises(DownloadError, match="folder"):
        make_client(handler).download_content(drive_item_from_graph(folder_item()))


def test_download_rejects_declared_size_over_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    item = drive_item_from_graph(sharepoint_item(size=11))
    with pytest.raises(DownloadError, match="limit"):
        make_client(handler, settings=graph_settings(max_download_bytes=10)).download_content(item)


def test_download_enforces_limit_while_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"x" * 50))  # no Content-Length

    item = drive_item_from_graph(onedrive_item(size=None))
    client = make_client(handler, settings=graph_settings(max_download_bytes=10))
    with pytest.raises(DownloadError, match="limit"):
        client.download_content(item)


def test_download_enforces_content_length_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 50)

    item = drive_item_from_graph(onedrive_item(size=None))
    with pytest.raises(DownloadError, match="limit"):
        make_client(handler, settings=graph_settings(max_download_bytes=10)).download_content(item)


def test_download_http_error_is_download_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return graph_error(410, "gone", "download url expired")

    with pytest.raises(DownloadError, match="expired"):
        make_client(handler).download_content(drive_item_from_graph(sharepoint_item()))


def test_download_transport_error_is_download_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DownloadError, match="network error"):
        make_client(handler).download_content(drive_item_from_graph(sharepoint_item()))


def test_close_does_not_close_injected_http_client() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=onedrive_item())))
    with SharePointClient(CountingAuth(), graph_settings(), http_client=http):
        pass
    assert not http.is_closed


def test_owned_http_client_uses_timeout_and_is_closed() -> None:
    client = SharePointClient(CountingAuth(), graph_settings(graph_timeout_seconds=12.5))
    http = client._http
    assert http.timeout.read == 12.5
    client.close()
    assert http.is_closed
