"""User-facing application.

``ZohoLoginApp`` is intentionally thin: it prompts the user, runs
``AuthService.login``, formats the result, and translates exceptions into
process exit codes. Anything richer (subcommands, an HTTP API, a desktop UI)
should compose ``ServiceContainer`` directly rather than extending this class.
"""

from __future__ import annotations

import sys
from typing import Optional

from .container import ServiceContainer
from .exceptions import (
    ZohoAuthError,
    ZohoCallbackTimeoutError,
    ZohoConfigError,
    ZohoError,
    ZohoPolicyError,
    ZohoTransportError,
)
from .models import ZohoUserSession


class ZohoLoginApp:
    """Terminal application built on top of a ``ServiceContainer``."""

    EXIT_OK = 0
    EXIT_FAILURE = 1
    EXIT_INTERRUPTED = 130

    def __init__(
        self,
        container: Optional[ServiceContainer] = None,
        *,
        open_browser: bool = True,
        prompt_before_browser: bool = True,
        force_browser: bool = False,
    ) -> None:
        self._container = container or ServiceContainer()
        self._open_browser = open_browser
        self._prompt_before_browser = prompt_before_browser
        self._force_browser = force_browser

    @property
    def container(self) -> ServiceContainer:
        return self._container

    def run(self) -> int:
        self._print_banner()

        if not self._force_browser and self._has_recent_saved_session():
            self._print_resume_notice()
        else:
            self._maybe_prompt_before_browser()

        try:
            session = self._container.auth_service.login_or_resume(
                open_browser=self._open_browser,
                force_browser=self._force_browser,
            )
        except KeyboardInterrupt:
            print("\nCancelled by user.", flush=True)
            return self.EXIT_INTERRUPTED
        except ZohoCallbackTimeoutError as exc:
            self._container.logger.error(str(exc))
            return self.EXIT_FAILURE
        except ZohoPolicyError as exc:
            self._container.logger.error(f"Access denied: {exc}")
            return self.EXIT_FAILURE
        except (ZohoAuthError, ZohoTransportError, ZohoConfigError) as exc:
            self._container.logger.error(f"Login failed: {exc}")
            return self.EXIT_FAILURE
        except ZohoError as exc:
            self._container.logger.error(f"Unexpected Zoho error: {exc}")
            return self.EXIT_FAILURE

        self._print_session(session)
        return self.EXIT_OK

    def _maybe_prompt_before_browser(self) -> None:
        if not self._prompt_before_browser or not sys.stdin.isatty():
            return
        print(
            "\nYou will sign in on Zoho's website in your browser.\n"
            "Your email and password are entered only there — not in this terminal.",
            flush=True,
        )
        try:
            input("\nPress Enter to continue... ")
        except EOFError:
            pass

    def _has_recent_saved_session(self) -> bool:
        store = self._container.token_store
        try:
            saved = store.load()
        except Exception:
            return False
        if saved is None:
            return False
        return not saved.is_older_than_days(self._container.config.session_max_age_days)

    def _print_resume_notice(self) -> None:
        days = self._container.config.session_max_age_days
        print(
            f"\nA saved session was found (valid for up to {days} day(s)). "
            "Refreshing silently — no browser sign-in needed.",
            flush=True,
        )

    @staticmethod
    def _print_banner() -> None:
        line = "=" * 52
        print(line, flush=True)
        print(" Zoho OAuth Login (terminal — live steps)", flush=True)
        print(line, flush=True)

    @staticmethod
    def _print_session(session: ZohoUserSession) -> None:
        line = "-" * 52
        print("\n" + line, flush=True)
        print(" Login successful", flush=True)
        print(line, flush=True)
        print(f"  Email         : {session.email}", flush=True)
        print(f"  Display name  : {session.display_name}", flush=True)
        print(f"  Token type    : {session.tokens.token_type}", flush=True)
        print(f"  Expires in    : {session.tokens.expires_in} seconds", flush=True)
        print(
            f"  Has refresh   : {'yes' if session.refresh_token else 'no'}",
            flush=True,
        )
        print(line, flush=True)
        print("\nFull user info payload:", flush=True)
        for key, value in session.user_info.items():
            print(f"  {key}: {value}", flush=True)
