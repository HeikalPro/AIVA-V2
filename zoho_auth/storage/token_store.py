"""Token storage contract and the always-empty default implementation."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class StoredSession:
    """Snapshot of a Zoho session persisted between runs.

    ``saved_at`` is a Unix timestamp captured the moment a session was written.
    The auth service uses it (together with ``ZohoConfig.session_max_age_days``)
    to decide whether a stored refresh token may still be reused silently or
    whether the user must sign in interactively again.
    """

    refresh_token: str
    access_token: Optional[str] = None
    expires_in: int = 0
    token_type: str = "Bearer"
    saved_at: float = field(default_factory=time.time)
    user_info: dict = field(default_factory=dict)

    def age_seconds(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.saved_at

    def is_older_than_days(self, max_age_days: int, now: Optional[float] = None) -> bool:
        if max_age_days <= 0:
            return False
        return self.age_seconds(now) > max_age_days * 86400


class TokenStore(Protocol):
    """Anything that can persist and retrieve a single ``StoredSession``."""

    def load(self) -> Optional[StoredSession]: ...

    def save(self, session: StoredSession) -> None: ...

    def clear(self) -> None: ...


class NullTokenStore:
    """No-op store. Use this when you intentionally do not want persistence."""

    def load(self) -> Optional[StoredSession]:
        return None

    def save(self, session: StoredSession) -> None:
        return

    def clear(self) -> None:
        return
