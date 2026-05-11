"""Dependency-injection container for the Zoho auth package.

The container constructs every collaborator with sane defaults and exposes one
``build_*`` factory method per dependency. To swap an implementation, subclass
``ServiceContainer`` and override the relevant factory.

Example — replace the HTTP client with a custom retry-enabled session:

    class MyContainer(ServiceContainer):
        def build_http(self):
            return RetryHttpClient(...)

    container = MyContainer()
    app = ZohoLoginApp(container)
"""

from __future__ import annotations

from typing import Optional

from .config import ConfigLoader, EnvConfigLoader, ZohoConfig
from .http import HttpClient, RequestsHttpClient
from .logging import ConsoleLogger, Logger
from .policy import AccessPolicy, AllowAllPolicy, EmailDomainPolicy
from .services.auth_service import AuthService
from .services.profile_service import ProfileService
from .services.token_service import TokenService
from .storage import JsonFileTokenStore, NullTokenStore, TokenStore


class ServiceContainer:
    """Default wiring of every service. Subclass to customise."""

    def __init__(
        self,
        *,
        config: Optional[ZohoConfig] = None,
        config_loader: Optional[ConfigLoader] = None,
        logger: Optional[Logger] = None,
        http: Optional[HttpClient] = None,
        policy: Optional[AccessPolicy] = None,
        token_store: Optional[TokenStore] = None,
    ) -> None:
        self._config_override = config
        self._config_loader_override = config_loader
        self._logger_override = logger
        self._http_override = http
        self._policy_override = policy
        self._token_store_override = token_store

        self.config: ZohoConfig = self._config_override or self.build_config()
        self.logger: Logger = self._logger_override or self.build_logger()
        self.http: HttpClient = self._http_override or self.build_http()
        self.policy: AccessPolicy = self._policy_override or self.build_policy()
        self.token_store: TokenStore = (
            self._token_store_override or self.build_token_store()
        )
        self.token_service: TokenService = self.build_token_service()
        self.profile_service: ProfileService = self.build_profile_service()
        self.auth_service: AuthService = self.build_auth_service()

    def build_config(self) -> ZohoConfig:
        loader = self._config_loader_override or EnvConfigLoader()
        return loader.load()

    def build_logger(self) -> Logger:
        return ConsoleLogger()

    def build_http(self) -> HttpClient:
        return RequestsHttpClient()

    def build_policy(self) -> AccessPolicy:
        if self.config.allowed_email_domains:
            return EmailDomainPolicy(self.config.allowed_email_domains)
        return AllowAllPolicy()

    def build_token_store(self) -> TokenStore:
        path = self.config.token_store_path
        if not path:
            return NullTokenStore()
        return JsonFileTokenStore(path)

    def build_token_service(self) -> TokenService:
        return TokenService(self.config, self.http, self.logger)

    def build_profile_service(self) -> ProfileService:
        return ProfileService(self.config, self.http, self.logger)

    def build_auth_service(self) -> AuthService:
        return AuthService(
            config=self.config,
            token_service=self.token_service,
            profile_service=self.profile_service,
            policy=self.policy,
            logger=self.logger,
            token_store=self.token_store,
        )
