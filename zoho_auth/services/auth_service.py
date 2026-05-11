"""End-to-end OAuth orchestration.

``AuthService`` does not know how tokens are exchanged, how user info is
fetched, or how access policies are decided — it composes the dedicated
services for each of those steps. Replace any collaborator (``TokenService``,
``ProfileService``, ``AccessPolicy``, ``CallbackServer`` factory) without
modifying this class.
"""

from __future__ import annotations

import secrets
import urllib.parse
import webbrowser
from typing import Callable, Optional

from ..callback import CallbackServer
from ..config import ZohoConfig
from ..exceptions import ZohoAuthError, ZohoError, ZohoTransportError
from ..logging import Logger
from ..models import TokenBundle, ZohoUserSession
from ..policy import AccessPolicy
from ..storage import NullTokenStore, StoredSession, TokenStore
from .base import Service
from .profile_service import ProfileService
from .token_service import TokenService

CallbackServerFactory = Callable[[str, int, str], CallbackServer]


def _default_callback_factory(host: str, port: int, path: str) -> CallbackServer:
    return CallbackServer(host=host, port=port, path=path)


class AuthService(Service):
    """Coordinates the entire authorization-code login flow."""

    TOTAL_STEPS = 5

    def __init__(
        self,
        config: ZohoConfig,
        token_service: TokenService,
        profile_service: ProfileService,
        policy: AccessPolicy,
        logger: Logger,
        token_store: Optional[TokenStore] = None,
        callback_server_factory: CallbackServerFactory = _default_callback_factory,
    ) -> None:
        super().__init__(config, logger)
        self._token_service = token_service
        self._profile_service = profile_service
        self._policy = policy
        self._token_store: TokenStore = token_store or NullTokenStore()
        self._callback_server_factory = callback_server_factory

    def login(self, *, open_browser: bool = True, timeout: int = 300) -> ZohoUserSession:
        state = secrets.token_urlsafe(24)
        auth_url = self.build_auth_url(state)

        server = self._callback_server_factory(
            self._config.callback_host,
            self._config.callback_port,
            self._config.callback_path,
        )

        self._logger.step(1, self.TOTAL_STEPS, "Starting local callback listener...")
        self._logger.info(f"Listening for redirect at: {server.listen_url}")

        server.start()
        self._logger.step(2, self.TOTAL_STEPS, "Listener ready.")

        try:
            self._logger.step(3, self.TOTAL_STEPS, "Sign in on Zoho in your browser.")
            self._logger.info(
                "Your password is only entered on Zoho — never in this terminal."
            )
            self._logger.info("Authorization URL:")
            self._logger.info(auth_url)

            if open_browser:
                self._logger.info("Opening browser...")
                webbrowser.open(auth_url)
            else:
                self._logger.info("Open the URL above manually in your browser.")

            captured = server.wait_for_callback(
                timeout=timeout, on_waiting=self._on_waiting
            )
            self._logger.clear_progress()
        finally:
            server.stop()
            self._logger.step(4, self.TOTAL_STEPS, "Callback server stopped.")

        if "error" in captured:
            raise ZohoAuthError(
                f"Zoho returned an error: {captured.get('error')} "
                f"({captured.get('error_description', 'no description')})"
            )

        if captured.get("state") != state:
            raise ZohoAuthError("State mismatch detected; aborting (possible CSRF).")

        self._logger.info("Authorization response received.")
        self._logger.step(5, self.TOTAL_STEPS, "Exchanging authorization code for tokens...")
        tokens = self._token_service.exchange_code(captured["code"])
        self._logger.info("Tokens received. Fetching your Zoho profile...")
        user_info = self._profile_service.fetch(tokens.access_token)
        self._logger.info("Profile loaded.")

        session = ZohoUserSession(tokens=tokens, user_info=user_info)
        self._policy.evaluate(session)
        self._persist(session)
        return session

    def login_with_refresh_token(self, refresh_token: str) -> ZohoUserSession:
        """Silent login using a previously persisted refresh token."""

        tokens = self._token_service.refresh(refresh_token)
        carry_refresh = tokens.refresh_token or refresh_token
        user_info = self._profile_service.fetch(tokens.access_token)
        session = ZohoUserSession(
            tokens=TokenBundle(
                access_token=tokens.access_token,
                refresh_token=carry_refresh,
                expires_in=tokens.expires_in,
                token_type=tokens.token_type,
                raw=tokens.raw,
            ),
            user_info=user_info,
        )
        self._policy.evaluate(session)
        self._persist(session)
        return session

    def login_or_resume(
        self,
        *,
        open_browser: bool = True,
        timeout: int = 300,
        force_browser: bool = False,
    ) -> ZohoUserSession:
        """Try a silent refresh first; fall back to the full browser flow.

        Decision tree:

        1. If ``force_browser`` is True, ignore any saved session.
        2. If a saved session exists and is younger than
           ``ZohoConfig.session_max_age_days``, attempt a silent refresh.
        3. On any failure (expired, revoked, transport error) the saved session
           is cleared and the browser flow runs.
        """

        if not force_browser:
            saved = self._token_store.load()
            if saved is not None:
                if saved.is_older_than_days(self._config.session_max_age_days):
                    self._logger.info(
                        f"Saved session is older than {self._config.session_max_age_days} day(s); "
                        "starting a fresh sign-in."
                    )
                    self._token_store.clear()
                else:
                    try:
                        self._logger.info(
                            "Found saved session — refreshing access token silently..."
                        )
                        return self.login_with_refresh_token(saved.refresh_token)
                    except (ZohoAuthError, ZohoTransportError, ZohoError) as exc:
                        self._logger.error(
                            f"Silent refresh failed ({exc}); falling back to browser sign-in."
                        )
                        self._token_store.clear()

        return self.login(open_browser=open_browser, timeout=timeout)

    def logout(self) -> None:
        """Revoke the saved refresh token (best effort) and clear local storage."""

        saved = self._token_store.load()
        self._token_store.clear()
        if saved and saved.refresh_token:
            self._token_service.revoke(saved.refresh_token)
        self._logger.info("Local session cleared.")

    def build_auth_url(self, state: Optional[str] = None) -> str:
        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "scope": self._config.scope,
            "redirect_uri": self._config.redirect_uri,
            "access_type": "offline",
            "prompt": "consent",
            "state": state or secrets.token_urlsafe(24),
        }
        return f"{self._config.auth_url}?{urllib.parse.urlencode(params)}"

    def _on_waiting(self, elapsed: float) -> None:
        secs = int(elapsed)
        self._logger.progress(
            f"Waiting for Zoho redirect... {secs}s "
            f"(complete sign-in in the browser; Ctrl+C to cancel)"
        )

    def _persist(self, session: ZohoUserSession) -> None:
        if isinstance(self._token_store, NullTokenStore):
            return
        if not session.refresh_token:
            return
        try:
            self._token_store.save(
                StoredSession(
                    refresh_token=session.refresh_token,
                    access_token=session.access_token,
                    expires_in=session.tokens.expires_in,
                    token_type=session.tokens.token_type,
                    user_info=session.user_info,
                )
            )
        except ZohoError as exc:
            self._logger.error(f"Could not persist session: {exc}")
