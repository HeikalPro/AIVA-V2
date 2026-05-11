"""JSON-backed implementation of ``TokenStore``.

The file is written with a 0600 permission mask on POSIX systems so other
users on the machine cannot read the refresh token. On Windows the mask is
applied best-effort; rely on the user account boundary instead.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from ..exceptions import ZohoError
from .token_store import StoredSession


class JsonFileTokenStore:
    """Stores a single session as JSON at ``path``."""

    _SCHEMA_VERSION = 1

    def __init__(self, path: str) -> None:
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Optional[StoredSession]:
        if not self._path.exists():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ZohoError(f"Could not read token store at {self._path}: {exc}") from exc
        if not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        payload = data.get("session") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            return None
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            return None
        return StoredSession(
            refresh_token=refresh_token,
            access_token=payload.get("access_token"),
            expires_in=int(payload.get("expires_in", 0)),
            token_type=payload.get("token_type", "Bearer"),
            saved_at=float(payload.get("saved_at", 0.0)),
            user_info=dict(payload.get("user_info", {})),
        )

    def save(self, session: StoredSession) -> None:
        data = {
            "schema": self._SCHEMA_VERSION,
            "session": {
                "refresh_token": session.refresh_token,
                "access_token": session.access_token,
                "expires_in": session.expires_in,
                "token_type": session.token_type,
                "saved_at": session.saved_at,
                "user_info": session.user_info,
            },
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)
        self._restrict_permissions(self._path)

    def clear(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            return
