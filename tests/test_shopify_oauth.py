import base64
import hashlib
import hmac

import pytest
import requests

from app.shopify_oauth import (
    ShopifyOAuthError,
    ShopifyTokenRefreshError,
    build_authorize_url,
    exchange_code_for_token,
    is_valid_shop_domain,
    refresh_access_token,
    verify_callback_hmac,
    verify_webhook_hmac,
)

CLIENT_SECRET = "shh-its-a-secret"


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json = json_data or {}

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


# ---------- is_valid_shop_domain ----------


def test_is_valid_shop_domain_accepts_bare_myshopify_domain():
    assert is_valid_shop_domain("real-shop.myshopify.com") is True


def test_is_valid_shop_domain_rejects_suffix_bypass():
    assert is_valid_shop_domain("real-shop.myshopify.com.evil.example") is False


def test_is_valid_shop_domain_rejects_custom_domain():
    assert is_valid_shop_domain("example.com") is False


def test_is_valid_shop_domain_rejects_empty():
    assert is_valid_shop_domain("") is False


def test_is_valid_shop_domain_rejects_leading_dot():
    assert is_valid_shop_domain(".myshopify.com") is False


# ---------- build_authorize_url ----------


def test_build_authorize_url_contains_all_params():
    url = build_authorize_url("acme.myshopify.com", "client123", "read_orders,read_products", "https://app.tillbird.co.uk/oauth/callback", "nonce-abc")
    assert url.startswith("https://acme.myshopify.com/admin/oauth/authorize?")
    assert "client_id=client123" in url
    assert "state=nonce-abc" in url
    assert "read_orders" in url


# ---------- verify_callback_hmac ----------


def _sign_params(params: dict, secret: str) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def test_verify_callback_hmac_accepts_correctly_signed_params():
    params = {"shop": "acme.myshopify.com", "code": "abc123", "state": "nonce", "timestamp": "123456"}
    signed = {**params, "hmac": _sign_params(params, CLIENT_SECRET)}
    assert verify_callback_hmac(signed, CLIENT_SECRET) is True


def test_verify_callback_hmac_rejects_tampered_param():
    params = {"shop": "acme.myshopify.com", "code": "abc123", "state": "nonce", "timestamp": "123456"}
    signed = {**params, "hmac": _sign_params(params, CLIENT_SECRET)}
    signed["shop"] = "attacker.myshopify.com"
    assert verify_callback_hmac(signed, CLIENT_SECRET) is False


def test_verify_callback_hmac_rejects_wrong_secret():
    params = {"shop": "acme.myshopify.com", "code": "abc123", "state": "nonce", "timestamp": "123456"}
    signed = {**params, "hmac": _sign_params(params, "a-different-secret")}
    assert verify_callback_hmac(signed, CLIENT_SECRET) is False


def test_verify_callback_hmac_missing_hmac_rejects():
    assert verify_callback_hmac({"shop": "acme.myshopify.com"}, CLIENT_SECRET) is False


# ---------- verify_webhook_hmac ----------


def _sign_body(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def test_verify_webhook_hmac_accepts_correctly_signed_body():
    body = b'{"id": 123, "domain": "acme.myshopify.com"}'
    assert verify_webhook_hmac(body, _sign_body(body, CLIENT_SECRET), CLIENT_SECRET) is True


def test_verify_webhook_hmac_rejects_tampered_body():
    body = b'{"id": 123, "domain": "acme.myshopify.com"}'
    signature = _sign_body(body, CLIENT_SECRET)
    tampered = b'{"id": 999, "domain": "acme.myshopify.com"}'
    assert verify_webhook_hmac(tampered, signature, CLIENT_SECRET) is False


def test_verify_webhook_hmac_missing_signature_rejects():
    assert verify_webhook_hmac(b"{}", "", CLIENT_SECRET) is False


# ---------- exchange_code_for_token ----------


def test_exchange_code_for_token_returns_parsed_json(monkeypatch):
    drafted = {"access_token": "shpat_xxx", "scope": "read_orders,read_products", "expires_in": 3600, "refresh_token": "shprt_yyy", "refresh_token_expires_in": 7776000}
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(200, drafted))
    result = exchange_code_for_token("acme.myshopify.com", "id", "secret", "code123")
    assert result == drafted


def test_exchange_code_for_token_raises_on_network_error(monkeypatch):
    def _raise(*a, **k):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr("requests.post", _raise)
    with pytest.raises(ShopifyOAuthError):
        exchange_code_for_token("acme.myshopify.com", "id", "secret", "code123")


def test_exchange_code_for_token_raises_on_http_error(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(400, {}))
    with pytest.raises(ShopifyOAuthError):
        exchange_code_for_token("acme.myshopify.com", "id", "secret", "bad-code")


# ---------- refresh_access_token ----------


def test_refresh_access_token_returns_parsed_json(monkeypatch):
    drafted = {"access_token": "shpat_new", "scope": "read_orders,read_products", "expires_in": 3600}
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(200, drafted))
    result = refresh_access_token("acme.myshopify.com", "id", "secret", "shprt_yyy")
    assert result == drafted


def test_refresh_access_token_401_raises_with_status_code(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(401, {}))
    with pytest.raises(ShopifyTokenRefreshError) as exc_info:
        refresh_access_token("acme.myshopify.com", "id", "secret", "dead-refresh-token")
    assert exc_info.value.status_code == 401


def test_refresh_access_token_500_raises_with_status_code(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResponse(500, {}))
    with pytest.raises(ShopifyTokenRefreshError) as exc_info:
        refresh_access_token("acme.myshopify.com", "id", "secret", "refresh-token")
    assert exc_info.value.status_code == 500


def test_refresh_access_token_network_error_raises_oauth_error(monkeypatch):
    def _raise(*a, **k):
        raise requests.Timeout("slow")

    monkeypatch.setattr("requests.post", _raise)
    with pytest.raises(ShopifyOAuthError):
        refresh_access_token("acme.myshopify.com", "id", "secret", "refresh-token")
