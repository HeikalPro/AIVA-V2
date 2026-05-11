"""Exception hierarchy used across all Zoho services.

Catch ``ZohoError`` to handle anything raised by the package, or one of the
specific subclasses for finer-grained handling. New services should raise the
narrowest applicable subclass and only fall back to ``ZohoError`` when none of
the existing categories fit.
"""

from __future__ import annotations


class ZohoError(Exception):
    """Base class for every error raised by the zoho_auth package."""


class ZohoConfigError(ZohoError):
    """Raised when configuration is missing or invalid."""


class ZohoTransportError(ZohoError):
    """Raised when an outbound HTTP request fails or returns a bad status."""


class ZohoAuthError(ZohoError):
    """Raised when Zoho rejects an authorization or returns an error code."""


class ZohoPolicyError(ZohoError):
    """Raised when an access policy denies a session (e.g. wrong email domain)."""


class ZohoCallbackTimeoutError(ZohoError):
    """Raised when the local callback server does not receive a redirect in time."""
