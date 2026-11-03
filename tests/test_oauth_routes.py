import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from app.credential_encryption import decrypt, encrypt, generate_key
from app.extensions import db
from app.live_onboarding import store_id_for_shop
from app.models import OAuthState, ShopifyAppCredential, ShopifyOAuthCredential
from app.store_config import load_store_config

CLIENT_ID = "oauth-client-id"
CLIENT_SECRET = "oauth-client-secret"
ENCRYPTION_KEY = generate_key()
SHOP = "acme.myshopify.com"


@pytest.fixture(autouse=True)
def _configure_oauth(app_config):
    app_config.credential_encryption_key = ENCRYPTION_KEY
    app_config.public_base_url = "https://app.tillbird.co.uk"
    return app_config


def _sign_params(params: dict, secret: str = CLIENT_SECRET) -> str:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def _sign_body(body: bytes, secret: str = CLIENT_SECRET) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _insert_state(app, state="nonce-abc", shop=SHOP, created_at=None):
    with app.app_context():
        db.session.add(OAuthState(state=state, shop_domain=shop, created_at=created_at or datetime.now(timezone.utc)))
        db.session.commit()


def _insert_app_credential(app, shop=SHOP, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, plan="starter"):
    with app.app_context():
        db.session.add(
            ShopifyAppCredential(
                store_id=store_id_for_shop(shop),
                shop_domain=shop,
                client_id=client_id,
                encrypted_client_secret=encrypt(client_secret, ENCRYPTION_KEY),
                plan=plan,
            )
        )
        db.session.commit()


TOKEN_RESPONSE = {
    "access_token": "shpat_new_token",
    "scope": "read_orders,read_products",
    "expires_in": 3600,
    "refresh_token": "shprt_new_refresh",
    "refresh_token_expires_in": 7776000,
}


# ---------- /oauth/install ----------


def test_install_redirects_to_shopify_authorize(client, app):
    _insert_app_credential(app)
    response = client.get(f"/oauth/install?shop={SHOP}")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith(f"https://{SHOP}/admin/oauth/authorize?")
    assert f"client_id={CLIENT_ID}" in location
    with app.app_context():
        assert db.session.query(OAuthState).filter_by(shop_domain=SHOP).count() == 1


def test_install_rejects_invalid_shop_domain(client):
    response = client.get("/oauth/install?shop=not-a-real-domain.com")
    assert response.status_code == 400


def test_install_errors_when_no_app_credential_registered(client, app):
    """scripts/add_client_app.py hasn't been run for this shop yet."""
    response = client.get(f"/oauth/install?shop={SHOP}")
    assert response.status_code == 500


def test_install_errors_when_public_base_url_not_configured(client, app, app_config):
    _insert_app_credential(app)
    app_config.public_base_url = ""
    response = client.get(f"/oauth/install?shop={SHOP}")
    assert response.status_code == 500


def test_install_purges_expired_states(client, app, app_config):
    _insert_app_credential(app)
    stale = datetime.now(timezone.utc) - timedelta(minutes=app_config.oauth_state_expiry_minutes + 5)
    _insert_state(app, state="stale-one", created_at=stale)
    client.get(f"/oauth/install?shop={SHOP}")
    with app.app_context():
        assert db.session.get(OAuthState, "stale-one") is None


def test_install_uses_this_shops_own_app_credential_not_some_other_shops(client, app):
    _insert_app_credential(app, shop="other-shop.myshopify.com", client_id="other-clients-id")
    response = client.get(f"/oauth/install?shop={SHOP}")
    assert response.status_code == 500  # nothing registered for SHOP itself


def test_install_accepts_shopifys_signed_app_url_open(client, app):
    """Simulates Shopify redirecting to this app's "App URL" right after
    a merchant installs via Shopify's own custom-distribution link (or
    reopens the app afterwards) — see the install route's docstring.
    These carry hmac/timestamp (and host, ignored) signed with THIS
    shop's own client secret, same algorithm as /oauth/callback's.
    """
    _insert_app_credential(app)
    params = {"shop": SHOP, "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvYWNtZQ", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/install", query_string=signed)
    assert response.status_code == 302
    assert response.headers["Location"].startswith(f"https://{SHOP}/admin/oauth/authorize?")


def test_install_rejects_bad_hmac_from_app_url_open(client, app):
    _insert_app_credential(app)
    params = {"shop": SHOP, "host": "YWRtaW4uc2hvcGlmeS5jb20vc3RvcmUvYWNtZQ", "timestamp": "1700000000"}
    response = client.get("/oauth/install", query_string={**params, "hmac": "0" * 64})
    assert response.status_code == 400


def test_install_rejects_app_url_open_signed_with_a_different_shops_secret(client, app):
    _insert_app_credential(app, client_secret="acme-own-secret")
    params = {"shop": SHOP, "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params, secret="some-other-secret")}
    response = client.get("/oauth/install", query_string=signed)
    assert response.status_code == 400


def test_install_without_hmac_still_works_as_a_plain_manual_link(client, app):
    """The link scripts/add_client_app.py prints has no hmac — must keep
    working exactly as before."""
    _insert_app_credential(app)
    response = client.get(f"/oauth/install?shop={SHOP}")
    assert response.status_code == 302


# ---------- /oauth/callback ----------


def test_callback_happy_path_stores_credential_and_renders_connected(client, app, monkeypatch):
    _insert_app_credential(app)
    _insert_state(app)
    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", lambda *a, **k: TOKEN_RESPONSE)
    monkeypatch.setattr("app.routes.oauth.spawn_provisioning_job", lambda *a, **k: None)

    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)

    assert response.status_code == 200
    assert b"connected" in response.data.lower() or b"Connected" in response.data

    with app.app_context():
        assert db.session.get(OAuthState, "nonce-abc") is None  # consumed
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert row is not None
        assert row.shop_domain == SHOP
        assert decrypt(row.encrypted_access_token, ENCRYPTION_KEY) == "shpat_new_token"
        assert decrypt(row.encrypted_refresh_token, ENCRYPTION_KEY) == "shprt_new_refresh"
        assert row.needs_reauth is False


def test_callback_spawns_provisioning_job_for_this_store_id_not_provision_live_store_directly(client, app, monkeypatch):
    """The callback must never call provision_live_store (or anything
    else that can run long) inline — only hand off to the background job,
    so it can return to the browser quickly regardless of how slow
    provisioning itself is."""
    _insert_app_credential(app, plan="growth")
    _insert_state(app)
    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", lambda *a, **k: TOKEN_RESPONSE)
    captured = {}
    monkeypatch.setattr("app.routes.oauth.spawn_provisioning_job", lambda store_id: captured.update(store_id=store_id))

    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)

    assert response.status_code == 200
    assert captured == {"store_id": "acme"}


def test_callback_does_not_actually_spawn_a_real_subprocess_by_default(client, app, monkeypatch):
    """Sanity check that the real spawn_provisioning_job (unmocked) uses
    Popen rather than something synchronous/blocking — mocks Popen itself
    so this test makes no real subprocess or API calls."""
    _insert_app_credential(app)
    _insert_state(app)
    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", lambda *a, **k: TOKEN_RESPONSE)
    captured = {}

    class _FakePopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr("app.routes.oauth.subprocess.Popen", _FakePopen)

    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)

    assert response.status_code == 200
    assert captured["args"][-1] == "acme"
    assert captured["args"][0] == sys.executable
    assert "provision_store.py" in captured["args"][1]
    assert captured["kwargs"]["start_new_session"] is True


def test_callback_uses_this_shops_own_secret_to_verify_and_exchange(client, app, monkeypatch):
    """A different client's app secret must never verify or be used for
    this shop's callback — each client's app is fully independent."""
    _insert_app_credential(app, client_id="acme-own-id", client_secret="acme-own-secret")
    _insert_state(app)
    captured = {}
    monkeypatch.setattr(
        "app.routes.oauth.exchange_code_for_token",
        lambda shop, client_id, client_secret, code: captured.update(client_id=client_id, client_secret=client_secret) or TOKEN_RESPONSE,
    )
    monkeypatch.setattr("app.routes.oauth.spawn_provisioning_job", lambda *a, **k: None)

    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params, secret="acme-own-secret")}
    response = client.get("/oauth/callback", query_string=signed)

    assert response.status_code == 200
    assert captured == {"client_id": "acme-own-id", "client_secret": "acme-own-secret"}


def test_callback_rejects_unknown_state(client, app):
    _insert_app_credential(app)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "never-existed", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400


def test_callback_rejects_state_for_a_different_shop(client, app):
    _insert_app_credential(app, shop="acme.myshopify.com")
    _insert_state(app, shop="acme.myshopify.com")
    params = {"shop": "different-shop.myshopify.com", "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400


def test_callback_rejects_expired_state(client, app, app_config):
    _insert_app_credential(app)
    stale = datetime.now(timezone.utc) - timedelta(minutes=app_config.oauth_state_expiry_minutes + 1)
    _insert_state(app, created_at=stale)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(OAuthState, "nonce-abc") is None


def test_callback_rejects_bad_hmac(client, app):
    _insert_app_credential(app)
    _insert_state(app)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    response = client.get("/oauth/callback", query_string={**params, "hmac": "0" * 64})
    assert response.status_code == 400


def test_callback_rejects_tampered_param_even_with_originally_valid_hmac(client, app):
    _insert_app_credential(app)
    _insert_state(app)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    signed["code"] = "a-different-code"
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400


def test_callback_rejects_invalid_shop_domain(client, app):
    params = {"shop": "evil.example", "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400


def test_callback_errors_when_no_app_credential_registered(client, app):
    """Defensive: shouldn't normally happen since /oauth/install already
    checks this, but the row could be removed between install and
    callback."""
    _insert_state(app)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 500


def test_callback_returns_502_when_token_exchange_fails(client, app, monkeypatch):
    _insert_app_credential(app)
    _insert_state(app)
    from app.shopify_oauth import ShopifyOAuthError

    def _raise(*a, **k):
        raise ShopifyOAuthError("network down")

    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", _raise)
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 502


def test_callback_rejects_incomplete_scope_grant(client, app, monkeypatch):
    _insert_app_credential(app)
    _insert_state(app)
    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", lambda *a, **k: {**TOKEN_RESPONSE, "scope": "read_orders"})
    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    response = client.get("/oauth/callback", query_string=signed)
    assert response.status_code == 400
    with app.app_context():
        assert db.session.get(ShopifyOAuthCredential, "acme") is None


def test_callback_updates_existing_credential_row_on_reinstall(client, app, monkeypatch):
    _insert_app_credential(app)
    with app.app_context():
        db.session.add(
            ShopifyOAuthCredential(
                store_id="acme",
                shop_domain=SHOP,
                encrypted_access_token=encrypt("old_token", ENCRYPTION_KEY),
                encrypted_refresh_token=encrypt("old_refresh", ENCRYPTION_KEY),
                scope="read_orders,read_products",
                access_token_expires_at=datetime.now(timezone.utc),
                refresh_token_expires_at=datetime.now(timezone.utc),
                needs_reauth=True,
            )
        )
        db.session.commit()
    _insert_state(app)
    monkeypatch.setattr("app.routes.oauth.exchange_code_for_token", lambda *a, **k: TOKEN_RESPONSE)
    monkeypatch.setattr("app.routes.oauth.spawn_provisioning_job", lambda *a, **k: None)

    params = {"shop": SHOP, "code": "auth-code-123", "state": "nonce-abc", "timestamp": "1700000000"}
    signed = {**params, "hmac": _sign_params(params)}
    client.get("/oauth/callback", query_string=signed)

    with app.app_context():
        row = db.session.get(ShopifyOAuthCredential, "acme")
        assert decrypt(row.encrypted_access_token, ENCRYPTION_KEY) == "shpat_new_token"
        assert row.needs_reauth is False


# ---------- /webhooks/app-uninstalled ----------


def test_uninstall_webhook_deletes_credential_and_marks_inactive(client, app, app_config):
    _insert_app_credential(app)
    with app.app_context():
        db.session.add(
            ShopifyOAuthCredential(
                store_id="acme",
                shop_domain=SHOP,
                encrypted_access_token=encrypt("tok", ENCRYPTION_KEY),
                encrypted_refresh_token=encrypt("ref", ENCRYPTION_KEY),
                scope="read_orders,read_products",
                access_token_expires_at=datetime.now(timezone.utc),
                refresh_token_expires_at=datetime.now(timezone.utc),
            )
        )
        db.session.commit()

    store_yaml = {
        "store_id": "acme",
        "store_name": "Acme",
        "shop_domain": SHOP,
        "public_domain": SHOP,
        "support_email": "support@acme.example",
        "tone": "Friendly",
        "brand_color": "#000",
        "greeting": "Hi",
        "hint_text": "Ask away",
        "allowed_origins": [],
        "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
        "forbidden": [],
        "mode": "live",
        "live_status": "active",
    }
    (app_config.stores_dir / "acme.yaml").write_text(yaml.safe_dump(store_yaml))

    body = json.dumps({"domain": SHOP}).encode()
    response = client.post(
        "/webhooks/app-uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": _sign_body(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200

    with app.app_context():
        assert db.session.get(ShopifyOAuthCredential, "acme") is None

    store = load_store_config("acme", stores_dir=app_config.stores_dir)
    assert store.live_status == "inactive"


def test_uninstall_webhook_rejects_bad_hmac(client, app):
    _insert_app_credential(app)
    body = json.dumps({"domain": SHOP}).encode()
    response = client.post(
        "/webhooks/app-uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": "bogus", "Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_uninstall_webhook_rejects_hmac_signed_with_a_different_clients_secret(client, app):
    """A forged shop-domain header signed with the WRONG client's secret
    must fail — each client's app secret only verifies its own shop."""
    _insert_app_credential(app, shop=SHOP, client_secret="acme-own-secret")
    _insert_app_credential(app, shop="other-shop.myshopify.com", client_secret="other-clients-secret")
    body = json.dumps({"domain": SHOP}).encode()
    response = client.post(
        "/webhooks/app-uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": _sign_body(body, secret="other-clients-secret"), "Content-Type": "application/json"},
    )
    assert response.status_code == 401


def test_uninstall_webhook_ok_when_no_app_credential_for_shop(client):
    body = json.dumps({"domain": "no-such-client.myshopify.com"}).encode()
    response = client.post(
        "/webhooks/app-uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": _sign_body(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200


def test_uninstall_webhook_ok_when_store_config_missing(client, app):
    _insert_app_credential(app)
    body = json.dumps({"domain": SHOP}).encode()
    response = client.post(
        "/webhooks/app-uninstalled",
        data=body,
        headers={"X-Shopify-Hmac-Sha256": _sign_body(body), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
