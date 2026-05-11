"""Domain value objects passed between services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TokenBundle:
    """Immutable container for the tokens returned by Zoho's token endpoint."""

    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    token_type: str
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict) -> "TokenBundle":
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token"),
            expires_in=int(payload.get("expires_in", 0)),
            token_type=payload.get("token_type", "Bearer"),
            raw=dict(payload),
        )


@dataclass
class ZohoUserSession:
    """Successful authentication result. Pass this around your app."""

    tokens: TokenBundle
    user_info: dict = field(default_factory=dict)

    @property
    def access_token(self) -> str:
        return self.tokens.access_token

    @property
    def refresh_token(self) -> Optional[str]:
        return self.tokens.refresh_token

    @property
    def email(self) -> Optional[str]:
        return (
            self.user_info.get("Email")
            or self.user_info.get("email")
            or self.user_info.get("primaryEmail")
        )

    @property
    def display_name(self) -> Optional[str]:
        return (
            self.user_info.get("Display_Name")
            or self.user_info.get("display_name")
            or self.user_info.get("name")
            or self.user_info.get("First_Name")
        )
