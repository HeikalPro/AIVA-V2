"""Local HTTP listener that catches the Zoho OAuth redirect.

This module only deals with transport concerns. It does not know what the
captured query parameters mean — that is the auth service's job. Any team
member who needs a different transport (e.g. SSH tunneling, a remote relay,
listening on a Unix socket) can implement an alternative class with the same
``start``/``wait_for_callback``/``stop`` surface.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

from .exceptions import ZohoCallbackTimeoutError, ZohoTransportError


class _CallbackHandler(BaseHTTPRequestHandler):
    """Internal request handler. Captures query params from the OAuth callback."""

    expected_path: str = "/"
    captured: dict = {}

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Not found.")
            return

        params = urllib.parse.parse_qs(parsed.query)
        for key in ("code", "state", "error", "error_description"):
            if key in params:
                self.captured[key] = params[key][0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if "error" in self.captured:
            body = (
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Authorization failed</h2>"
                f"<p>{self.captured.get('error')}</p>"
                "<p>You can close this tab and check the terminal.</p>"
                "</body></html>"
            )
        else:
            body = (
                "<html><body style='font-family:sans-serif;padding:2rem'>"
                "<h2>Login complete</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>"
            )
        self.wfile.write(body.encode("utf-8"))


class CallbackServer:
    """Single-shot local HTTP server that captures the OAuth redirect."""

    def __init__(self, host: str, port: int, path: str) -> None:
        self._host = host
        self._port = port
        self._path = path
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._handler_cls: Optional[type] = None

    @property
    def listen_url(self) -> str:
        return f"http://{self._host}:{self._port}{self._path}"

    def start(self) -> None:
        handler_cls = type(
            "BoundCallbackHandler",
            (_CallbackHandler,),
            {"expected_path": self._path, "captured": {}},
        )
        try:
            server = HTTPServer((self._host, self._port), handler_cls)
        except OSError as exc:
            raise ZohoTransportError(
                f"Could not bind callback server on {self._host}:{self._port} "
                f"({exc}). Is another process using the port?"
            ) from exc

        self._handler_cls = handler_cls
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

    def wait_for_callback(
        self,
        timeout: int = 300,
        *,
        on_waiting: Optional[Callable[[float], None]] = None,
        tick_seconds: float = 0.25,
    ) -> dict:
        if self._handler_cls is None:
            raise ZohoTransportError("Callback server has not been started.")

        deadline = time.time() + timeout
        start = time.time()
        last_progress = 0.0
        while time.time() < deadline:
            captured = self._handler_cls.captured
            if "code" in captured or "error" in captured:
                return dict(captured)
            elapsed = time.time() - start
            if on_waiting is not None and elapsed - last_progress >= 1.0:
                last_progress = elapsed
                on_waiting(elapsed)
            time.sleep(tick_seconds)
        raise ZohoCallbackTimeoutError(
            f"Timed out after {timeout}s waiting for the Zoho callback."
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
