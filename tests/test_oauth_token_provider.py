from datetime import datetime, timedelta, timezone

import pytest

from app.catalog import CatalogError
from app.credential_encryption import decrypt, encrypt, generate_key
from app.extensions import db
from app.models import ShopifyAppCredential, ShopifyOAuthCredential
from app.shopify_client import ShopifyAdminClient, _OAuthTokenProvider, build_client_for_store
from app.shopify_oauth import ShopifyTokenRefreshError
from app.store_config import StoreConfig

KEY = generate_key()

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


def _insert_credential(
    app, store_id="acme", shop_domain="acme.myshopify.com", expires_in_seconds=3600, needs_reauth=False, has_refresh_token=True
):
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(
            ShopifyOAuthCredential(
                store_id=store_id,
                shop_domain=shop_domain,
                encrypted_access_token=encrypt("shpat_current", KEY),
                encrypted_refresh_token=encrypt("shprt_refresh", KEY) if has_refresh_token else b"",
                scope="read_orders,read_products",
                access_token_expires_at=now + timedelta(seconds=expires_in_seconds),
                refresh_token_expires_at=now + timedelta(days=90),
                needs_reauth=needs_reauth,
            )
        )
        db.session.commit()


def _insert_app_credential(app, store_id="acme", shop_domain="acme.myshopify.com", client_id="oauth-client-id"):
    with app.app_context():
        db.session.add(
            ShopifyAppCredential(
                store_id=store_id,
                shop_domain=shop_domain,
                client_id=client_id,
                encrypted_client_secret=encrypt("oauth-client-secret", KEY),
                plan="starter",
            )
        )
        db.session.commit()


def _provider(store_id="acme", shop_domain="acme.myshopify.com"):
    return _OAuthTokenProvider(store_id=store_id, shop_domain=shop_domain, encryption_key=KEY)


# ---------- _OAuthTokenProvider ----------


def test_get_token_returns_decrypted_token_when_not_near_expiry(app):
    _insert_credential(app, expires_in_seconds=3600)
    with app.app_context():
        assert _provider().get_token() == "shpat_current"


def test_get_token_raises_when_no_credential_stored(app):
    with app.app_context():
        with pytest.raises(CatalogError, match="no Shopify OAuth credential"):
            _provider().get_token()


def test_get_token_raises_when_needs_reauth(app):
    _insert_credential(app, needs_reauth=True)
    with app.app_context():
        with pytest.raises(CatalogError, match="needs reauthorization"):
            _provider().get_token()


def test_get_token_raises_when_no_app_credential_stored(app):
    """A dead/missing ShopifyAppCredential row (e.g. scripts/add_client_app.py
    was never run for this store) must fail clearly, before even
    attempting a refresh call with no client_id/secret to use.
    """
    _insert_credential(app, expires_in_seconds=30)
    with app.app_context():
        with pytest.raises(CatalogError, match="no Shopify app credential"):
            _provider().get_token()


def test_get_token_skips_refresh_when_no_refresh_token_stored(app, monkeypatch):
    """A row with no refresh token (e.g. a non-expiring token from before
    this app requested expiring=1, or hand-imported) has nothing to
    refresh with — even well past its stored expiry, get_token() must
    just return the access token as-is rather than calling the refresh
    endpoint (which would need a refresh_token it doesn't have).
    """
    _insert_credential(app, expires_in_seconds=-3600, has_refresh_token=False)  # already "expired"

    def _fail(*a, **k):
        raise AssertionError("refresh_access_token should not be called when there's no refresh token")

    monkeypatch.setattr("app.shopify_client.refresh_access_token", _fail)
    with app.app_context():
        assert _provider().get_token() == "shpat_current"


def test_get_token_refreshes_when_near_expiry(app, monkeypatch):
    _insert_credential(app, expires_in_seconds=30)  # within the refresh margin
    _insert_app_credential(app)
    monkeypatch.setattr(
        "app.shopify_client.refresh_access_token",
        lambda *a, **k: {"access_token": "shpat_refreshed", "expires_in": 3600},
    )
    with app.app_context():
        token = _provider().get_token()
        assert token == "shpat_refreshed"
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert decrypt(row.encrypted_access_token, KEY) == "shpat_refreshed"


def test_get_token_refresh_uses_this_stores_own_app_credential(app, monkeypatch):
    _insert_credential(app, expires_in_seconds=30)
    _insert_app_credential(app, client_id="this-clients-own-id")
    captured = {}
    monkeypatch.setattr(
        "app.shopify_client.refresh_access_token",
        lambda shop, client_id, client_secret, refresh_token, timeout=10.0: captured.update(client_id=client_id, client_secret=client_secret)
        or {"access_token": "shpat_refreshed", "expires_in": 3600},
    )
    with app.app_context():
        _provider().get_token()
    assert captured["client_id"] == "this-clients-own-id"
    assert captured["client_secret"] == "oauth-client-secret"


def test_get_token_refresh_rotates_refresh_token_when_given_a_new_one(app, monkeypatch):
    _insert_credential(app, expires_in_seconds=30)
    _insert_app_credential(app)
    monkeypatch.setattr(
        "app.shopify_client.refresh_access_token",
        lambda *a, **k: {"access_token": "shpat_refreshed", "expires_in": 3600, "refresh_token": "shprt_new", "refresh_token_expires_in": 7776000},
    )
    with app.app_context():
        _provider().get_token()
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert decrypt(row.encrypted_refresh_token, KEY) == "shprt_new"


def test_get_token_refresh_401_marks_needs_reauth_and_raises(app, monkeypatch):
    _insert_credential(app, expires_in_seconds=30)
    _insert_app_credential(app)

    def _raise(*a, **k):
        raise ShopifyTokenRefreshError(401, "dead refresh token")

    monkeypatch.setattr("app.shopify_client.refresh_access_token", _raise)
    with app.app_context():
        with pytest.raises(CatalogError):
            _provider().get_token()
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert row.needs_reauth is True


def test_get_token_refresh_500_does_not_mark_needs_reauth(app, monkeypatch):
    _insert_credential(app, expires_in_seconds=30)
    _insert_app_credential(app)

    def _raise(*a, **k):
        raise ShopifyTokenRefreshError(500, "transient failure")

    monkeypatch.setattr("app.shopify_client.refresh_access_token", _raise)
    with app.app_context():
        with pytest.raises(CatalogError):
            _provider().get_token()
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert row.needs_reauth is False


# ---------- build_client_for_store wiring ----------


class _FakeAppConfig:
    shopify_api_version = "2026-07"
    credential_encryption_key = KEY


def test_build_client_for_store_uses_oauth_credential_when_present(app):
    _insert_credential(app)
    store = StoreConfig.model_validate(BASE_STORE)
    with app.app_context():
        client = build_client_for_store(store, _FakeAppConfig())
        assert isinstance(client._token_provider, _OAuthTokenProvider)
        assert client._token_provider.get_token() == "shpat_current"


def test_build_client_for_store_raises_when_no_oauth_credential_either(app):
    store = StoreConfig.model_validate(BASE_STORE)
    with app.app_context():
        with pytest.raises(CatalogError, match="no Shopify credentials configured"):
            build_client_for_store(store, _FakeAppConfig())
