from __future__ import annotations

from typing import Any

import pytest

from crm_ingestion.connectors.sharepoint import ClientCredentialsAuthProvider, StaticTokenAuthProvider
from crm_ingestion.errors import AuthenticationError, ConfigurationError

from .fakes import SECRET, FakeMsalApp, graph_settings


def configured() -> Any:
    return graph_settings(tenant_id="tenant-guid", client_id="client-guid", client_secret=SECRET)


def test_static_token_provider() -> None:
    provider = StaticTokenAuthProvider("abc")
    assert provider.get_access_token() == "abc"
    provider.invalidate()  # default no-op
    assert provider.get_access_token() == "abc"


def test_unconfigured_raises_at_token_time_listing_missing_vars() -> None:
    provider = ClientCredentialsAuthProvider(graph_settings(client_id="set"))  # construction is fine
    with pytest.raises(ConfigurationError) as info:
        provider.get_access_token()
    message = str(info.value)
    assert "MICROSOFT_TENANT_ID" in message
    assert "MICROSOFT_CLIENT_SECRET" in message
    assert "MICROSOFT_CLIENT_ID" not in message


def test_uses_injected_msal_app() -> None:
    created: list[FakeMsalApp] = []

    def factory(client_id: str, **kwargs: Any) -> FakeMsalApp:
        app = FakeMsalApp({"access_token": "tok-1", "expires_in": 3599}, client_id=client_id, **kwargs)
        created.append(app)
        return app

    provider = ClientCredentialsAuthProvider(configured(), app_factory=factory)
    assert provider.get_access_token() == "tok-1"
    assert provider.get_access_token() == "tok-1"
    assert len(created) == 1  # app reused; msal owns the token cache
    app = created[0]
    assert app.init_kwargs == {
        "client_id": "client-guid",
        "authority": "https://login.microsoftonline.com/tenant-guid",
        "client_credential": SECRET,
    }
    assert app.calls == [["https://graph.microsoft.com/.default"]] * 2


def test_invalidate_rebuilds_app() -> None:
    created: list[FakeMsalApp] = []

    def factory(client_id: str, **kwargs: Any) -> FakeMsalApp:
        created.append(FakeMsalApp({"access_token": f"tok-{len(created)}"}))
        return created[-1]

    provider = ClientCredentialsAuthProvider(configured(), app_factory=factory)
    assert provider.get_access_token() == "tok-0"
    provider.invalidate()
    assert provider.get_access_token() == "tok-1"


def test_msal_error_dict_raises_authentication_error_without_secret() -> None:
    result = {
        "error": "invalid_client",
        "error_description": f"AADSTS7000215: Invalid client secret provided ({SECRET}).",
    }
    provider = ClientCredentialsAuthProvider(configured(), app_factory=lambda *a, **k: FakeMsalApp(result))
    with pytest.raises(AuthenticationError) as info:
        provider.get_access_token()
    message = str(info.value)
    assert "invalid_client" in message
    assert "AADSTS7000215" in message
    assert SECRET not in message
    assert info.value.__cause__ is None


def test_msal_exception_is_wrapped_without_secret() -> None:
    class Boom:
        def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
            raise RuntimeError(f"network down while sending {SECRET}")

    provider = ClientCredentialsAuthProvider(configured(), app_factory=lambda *a, **k: Boom())
    with pytest.raises(AuthenticationError) as info:
        provider.get_access_token()
    assert SECRET not in str(info.value)
    assert "network down" in str(info.value)


def test_missing_msal_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "msal":
            raise ImportError("No module named 'msal'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ConfigurationError, match=r"crm-document-ingestion\[msal\]"):
        ClientCredentialsAuthProvider(configured()).get_access_token()
