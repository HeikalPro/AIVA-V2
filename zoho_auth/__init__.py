"""Zoho OAuth authentication package.

Public surface for application code:

    from zoho_auth import ServiceContainer, ZohoLoginApp

For team members adding new services, see ``zoho_auth/services/base.py`` and
``zoho_auth/container.py``. The container exposes ``build_*`` factory methods
so any default service can be replaced by subclassing the container.
"""

from typing import Optional, Sequence

from .config import ZohoConfig, EnvConfigLoader, ConfigLoader
from .container import ServiceContainer
from .exceptions import (
    ZohoAuthError,
    ZohoCallbackTimeoutError,
    ZohoConfigError,
    ZohoError,
    ZohoPolicyError,
    ZohoTransportError,
)
from .logging import ConsoleLogger, Logger, NullLogger
from .models import TokenBundle, ZohoUserSession
from .policy import AccessPolicy, AllowAllPolicy, EmailDomainPolicy
from .services.auth_service import AuthService
from .services.profile_service import ProfileService
from .services.token_service import TokenService
from .storage import JsonFileTokenStore, NullTokenStore, StoredSession, TokenStore
from .app import ZohoLoginApp
from .cli import main as cli_main


def login(argv: Optional[Sequence[str]] = None) -> int:
    """Single-call public entrypoint for library consumers."""
    return cli_main(argv)

__all__ = [
    "AccessPolicy",
    "AllowAllPolicy",
    "AuthService",
    "ConfigLoader",
    "ConsoleLogger",
    "EmailDomainPolicy",
    "EnvConfigLoader",
    "JsonFileTokenStore",
    "login",
    "Logger",
    "NullLogger",
    "NullTokenStore",
    "ProfileService",
    "ServiceContainer",
    "StoredSession",
    "TokenBundle",
    "TokenService",
    "TokenStore",
    "ZohoAuthError",
    "ZohoCallbackTimeoutError",
    "ZohoConfig",
    "ZohoConfigError",
    "ZohoError",
    "ZohoLoginApp",
    "ZohoPolicyError",
    "ZohoTransportError",
    "ZohoUserSession",
]
