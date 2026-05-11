"""HTTP transport abstraction.

Services depend on the ``HttpClient`` protocol, not on ``requests`` directly.
This makes services trivially testable (inject a stub) and lets the team plug
in any HTTP library — ``httpx``, ``aiohttp`` (with a sync wrapper), an
authenticated session, retry middleware, etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol

import requests

from .exceptions import ZohoTransportError


@dataclass
class HttpResponse:
    """Minimal response object decoupled from any HTTP library."""

    status_code: int
    text: str
    _payload: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        if self._payload is not None:
            return self._payload
        import json as _json

        try:
            return _json.loads(self.text)
        except ValueError as exc:
            raise ZohoTransportError(
                f"Response was not valid JSON: {self.text[:200]}"
            ) from exc


class HttpClient(Protocol):
    """Structural interface every HTTP client must satisfy."""

    def get(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: int = 30,
    ) -> HttpResponse: ...

    def post(
        self,
        url: str,
        *,
        data: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: int = 30,
    ) -> HttpResponse: ...


class RequestsHttpClient:
    """Default ``HttpClient`` backed by the ``requests`` library."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()

    def get(
        self,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        timeout: int = 30,
    ) -> HttpResponse:
        return self._wrap(
            self._session.get(url, headers=dict(headers or {}), timeout=timeout)
        )

    def post(
        self,
        url: str,
        *,
        data: Optional[Mapping[str, str]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: int = 30,
    ) -> HttpResponse:
        return self._wrap(
            self._session.post(
                url,
                data=dict(data or {}),
                headers=dict(headers or {}),
                timeout=timeout,
            )
        )

    @staticmethod
    def _wrap(response: requests.Response) -> HttpResponse:
        payload: Optional[dict] = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return HttpResponse(
            status_code=response.status_code,
            text=response.text,
            _payload=payload,
        )
