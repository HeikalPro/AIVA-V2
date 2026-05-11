"""Access policy abstractions.

A policy is a small object that decides whether an authenticated session is
permitted to use the application. The default ``EmailDomainPolicy`` enforces a
list of allowed email domains read from configuration. Add new policy classes
(e.g. role-based, group-based) by implementing the ``AccessPolicy`` protocol
and wiring them through the container.
"""

from __future__ import annotations

from typing import Iterable, Protocol, Tuple

from .exceptions import ZohoPolicyError
from .models import ZohoUserSession


class AccessPolicy(Protocol):
    """Decision object. Raise ``ZohoPolicyError`` to deny."""

    def evaluate(self, session: ZohoUserSession) -> None: ...


class AllowAllPolicy:
    """Default policy when no constraints are configured."""

    def evaluate(self, session: ZohoUserSession) -> None:
        return


class EmailDomainPolicy:
    """Allow the session only if the email matches one of the configured domains."""

    def __init__(self, allowed_domains: Iterable[str]) -> None:
        self._domains: Tuple[str, ...] = tuple(d.lower().lstrip("@") for d in allowed_domains if d)

    @property
    def allowed_domains(self) -> Tuple[str, ...]:
        return self._domains

    def evaluate(self, session: ZohoUserSession) -> None:
        if not self._domains:
            return
        email = (session.email or "").lower()
        if not any(email.endswith("@" + d) for d in self._domains):
            allowed = ", ".join("@" + d for d in self._domains)
            raise ZohoPolicyError(
                f"Email '{email or 'unknown'}' is not in the allowed domain(s): {allowed}."
            )
