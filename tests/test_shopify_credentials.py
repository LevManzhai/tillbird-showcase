import pytest

from app.catalog import CatalogError
from app.shopify_client import (
    ShopifyAdminClient,
    _ClientCredentialsTokenProvider,
    _StaticTokenProvider,
    build_client_for_store,
    fetch_client_credentials_token,
)
from app.store_config import StoreConfig, StoreConfigError

BASE_STORE = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": [],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "live",
}


class _FakeAppConfig:
    shopify_api_version = "2026-07"


def test_store_config_no_credential_mechanism_means_oauth():
    """A live store with none of admin_token_env/client_id_env/
    client_secret_env set is assumed to use the OAuth credentials table
    (see app.shopify_client.build_client_for_store) — this no longer
    raises."""
    store = StoreConfig.model_validate({**BASE_STORE, "admin_token_env": None})
    assert store.uses_oauth_credentials() is True


def test_store_config_client_id_and_secret_env_must_be_paired():
    with pytest.raises(Exception, match="together"):
        StoreConfig.model_validate({**BASE_STORE, "client_id_env": "SHOPIFY_CLIENT_ID_ACME"})


def test_store_config_accepts_client_credentials_alone():
    store = StoreConfig.model_validate(
        {**BASE_STORE, "client_id_env": "SHOPIFY_CLIENT_ID_ACME", "client_secret_env": "SHOPIFY_CLIENT_SECRET_ACME"}
    )
    assert store.uses_client_credentials() is True
    assert store.admin_token_env is None


def test_get_shopify_client_credentials_reads_env_vars(monkeypatch):
    store = StoreConfig.model_validate(
        {**BASE_STORE, "client_id_env": "SHOPIFY_CLIENT_ID_ACME", "client_secret_env": "SHOPIFY_CLIENT_SECRET_ACME"}
    )
    monkeypatch.setenv("SHOPIFY_CLIENT_ID_ACME", "id123")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET_ACME", "secret456")
    client_id, client_secret = store.get_shopify_client_credentials()
    assert (client_id, client_secret) == ("id123", "secret456")


def test_get_shopify_client_credentials_missing_env_var_raises(monkeypatch):
    store = StoreConfig.model_validate(
        {**BASE_STORE, "client_id_env": "SHOPIFY_CLIENT_ID_ACME", "client_secret_env": "SHOPIFY_CLIENT_SECRET_ACME"}
    )
    monkeypatch.delenv("SHOPIFY_CLIENT_ID_ACME", raising=False)
    monkeypatch.delenv("SHOPIFY_CLIENT_SECRET_ACME", raising=False)
    with pytest.raises(StoreConfigError, match="not both set"):
        store.get_shopify_client_credentials()


def test_build_client_for_store_prefers_static_token(monkeypatch):
    store = StoreConfig.model_validate({**BASE_STORE, "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME"})
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test")
    client = build_client_for_store(store, _FakeAppConfig())
    assert isinstance(client._token_provider, _StaticTokenProvider)
    assert client._token_provider.get_token() == "shpat_test"


def test_build_client_for_store_uses_client_credentials(monkeypatch):
    store = StoreConfig.model_validate(
        {**BASE_STORE, "client_id_env": "SHOPIFY_CLIENT_ID_ACME", "client_secret_env": "SHOPIFY_CLIENT_SECRET_ACME"}
    )
    monkeypatch.setenv("SHOPIFY_CLIENT_ID_ACME", "id123")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET_ACME", "secret456")
    client = build_client_for_store(store, _FakeAppConfig())
    assert isinstance(client._token_provider, _ClientCredentialsTokenProvider)


def test_fetch_client_credentials_token_posts_expected_request(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "tok_abc", "scope": "read_orders", "expires_in": 86399}

    def fake_post(url, data, timeout):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse()

    monkeypatch.setattr("app.shopify_client.requests.post", fake_post)
    result = fetch_client_credentials_token("acme.myshopify.com", "id123", "secret456")

    assert captured["url"] == "https://acme.myshopify.com/admin/oauth/access_token"
    assert captured["data"] == {"grant_type": "client_credentials", "client_id": "id123", "client_secret": "secret456"}
    assert result["access_token"] == "tok_abc"


def test_client_credentials_token_provider_caches_until_near_expiry(monkeypatch):
    calls = []

    def fake_fetch(shop_domain, client_id, client_secret, timeout=10.0):
        calls.append(1)
        return {"access_token": f"tok_{len(calls)}", "expires_in": 86399}

    monkeypatch.setattr("app.shopify_client.fetch_client_credentials_token", fake_fetch)
    provider = _ClientCredentialsTokenProvider("acme.myshopify.com", "id123", "secret456")

    first = provider.get_token()
    second = provider.get_token()
    assert first == second == "tok_1"
    assert len(calls) == 1


def test_client_credentials_token_provider_refreshes_near_expiry(monkeypatch):
    import time as time_module

    calls = []

    def fake_fetch(shop_domain, client_id, client_secret, timeout=10.0):
        calls.append(1)
        return {"access_token": f"tok_{len(calls)}", "expires_in": 86399}

    monkeypatch.setattr("app.shopify_client.fetch_client_credentials_token", fake_fetch)
    provider = _ClientCredentialsTokenProvider("acme.myshopify.com", "id123", "secret456")

    first = provider.get_token()
    # Simulate time passing to within the refresh margin of expiry.
    monkeypatch.setattr(time_module, "time", lambda: provider._expires_at - 1)
    second = provider.get_token()

    assert first == "tok_1"
    assert second == "tok_2"
    assert len(calls) == 2


def test_shopify_admin_client_requires_some_credential():
    with pytest.raises(CatalogError, match="requires access_token"):
        ShopifyAdminClient(shop_domain="acme.myshopify.com", api_version="2026-07")
