"""OAuth install flow for client stores — see docs/OAUTH_PLAN.md.

A custom-distribution Shopify app can only be installed on the ONE store
(or one Plus organization) it was generated for — so there's a SEPARATE
app per client (own client_id + client_secret), not one app shared by
every client. Each client's app credentials are registered once with
scripts/add_client_app.py (app.models.ShopifyAppCredential) before their
install link is sent; everything below looks that row up by shop domain
rather than reading a single global client_id/secret.

GET /oauth/install?shop=xxx.myshopify.com
    Entry point — BOTH the plain link scripts/add_client_app.py prints
    AND this app's own "App URL" (Shopify redirects here automatically
    right after a merchant installs via Shopify's own custom-distribution
    link, and every time they reopen the app afterwards — see the
    route's own docstring). Validates the shop domain, looks up that
    shop's own app credentials, verifies Shopify's signed params when
    present, stores a short-lived CSRF state nonce, redirects to
    Shopify's consent screen.

GET /oauth/callback
    Shopify redirects here after the client approves (or declines).
    Verifies state + HMAC (with THIS shop's own client secret), exchanges
    the code for tokens, saves them encrypted, and shows a "you're
    connected" page immediately — provisioning the store's YAML (scrape +
    knowledge-base draft, app.live_onboarding) is handed off to a
    detached scripts/provision_store.py process so this request never
    risks gunicorn's worker timeout.

POST /webhooks/app-uninstalled
    Shopify calls this when a client uninstalls. The shop domain in the
    request (untrusted until verified) selects which client's app secret
    to verify the HMAC with; once verified, deletes the stored tokens and
    marks the store inactive.
"""

import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from flask import Blueprint, current_app, redirect, render_template, request

from app.credential_encryption import decrypt, encrypt
from app.extensions import db
from app.live_onboarding import store_id_for_shop
from app.models import OAuthState, ShopifyAppCredential, ShopifyOAuthCredential
from app.shopify_oauth import (
    ShopifyOAuthError,
    build_authorize_url,
    exchange_code_for_token,
    is_valid_shop_domain,
    verify_callback_hmac,
    verify_webhook_hmac,
)
from app.store_config import load_store_config

oauth_bp = Blueprint("oauth", __name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVISION_SCRIPT = _REPO_ROOT / "scripts" / "provision_store.py"


def spawn_provisioning_job(store_id: str) -> None:
    """Runs scripts/provision_store.py <store_id> as a detached
    background process, so /oauth/callback can return the "You're
    connected" page to the browser in well under a second instead of
    waiting on the scrape + Claude knowledge-base draft (which can run
    well past gunicorn's worker timeout — see docs/DEPLOY.md section 14
    for the incident this fixed). start_new_session gives it its own
    session so it keeps running after this request — and this gunicorn
    worker — finish; provision_live_store (and this script) are
    idempotent, so a crash or a re-run (e.g. the merchant reopening the
    install link) is always safe to retry.
    """
    subprocess.Popen([sys.executable, str(_PROVISION_SCRIPT), store_id], start_new_session=True)


def _error(message: str, status: int = 400):
    return render_template("oauth_error.html", message=message), status


def _purge_expired_states(app_config) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=app_config.oauth_state_expiry_minutes)
    OAuthState.query.filter(OAuthState.created_at < cutoff).delete()
    db.session.commit()


def _state_expired(state_row: OAuthState, app_config) -> bool:
    created_at = state_row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > created_at + timedelta(minutes=app_config.oauth_state_expiry_minutes)


def _app_credential_for_shop(shop: str) -> ShopifyAppCredential | None:
    return ShopifyAppCredential.query.filter_by(shop_domain=shop).first()


@oauth_bp.get("/oauth/install")
def install():
    """Entry point for BOTH: (a) the plain `?shop=` link
    scripts/add_client_app.py prints, meant to be sent/opened by hand, and
    (b) this app's own "App URL" (see docs/DEPLOY.md section 14) — the
    page Shopify redirects a merchant's browser to right after they
    install via Shopify's own generated custom-distribution link, and
    every time they reopen the app from their Shopify admin afterwards.
    (b) is what makes a one-link install experience possible: without it,
    Shopify's own install link only grants the app visibility/consent on
    that store — it never calls back into this app at all (no_redirect),
    so we'd never get a token until the merchant separately opened this
    link by hand. Shopify's (b) requests carry `hmac`/`timestamp` (and
    `host`, unused — this app isn't embedded) signed with this shop's own
    app's client secret, verified the same way as /oauth/callback's;
    `?shop=`-only links from (a) have no `hmac` and skip that check,
    exactly as before.
    """
    app_config = current_app.extensions["app_config"]
    shop = request.args.get("shop", "").strip().lower()

    if not is_valid_shop_domain(shop):
        return _error("That link doesn't look right — the shop address isn't valid. Ask for a fresh install link.")
    if not app_config.public_base_url:
        current_app.logger.error("oauth/install: PUBLIC_BASE_URL not configured")
        return _error("Tillbird isn't set up to accept installs right now — please let us know.", status=500)

    app_credential = _app_credential_for_shop(shop)
    if app_credential is None:
        current_app.logger.error("oauth/install: no ShopifyAppCredential for shop=%s — run scripts/add_client_app.py first", shop)
        return _error("This store isn't set up for install yet — please let us know and we'll sort it.", status=500)

    if request.args.get("hmac"):
        client_secret = decrypt(app_credential.encrypted_client_secret, app_config.credential_encryption_key)
        if not verify_callback_hmac(request.args.to_dict(), client_secret):
            current_app.logger.warning("oauth/install: HMAC verification failed for shop=%s", shop)
            return _error("Could not verify this request.")

    _purge_expired_states(app_config)

    state = secrets.token_urlsafe(24)
    db.session.add(OAuthState(state=state, shop_domain=shop))
    db.session.commit()

    redirect_uri = f"{app_config.public_base_url}/oauth/callback"
    authorize_url = build_authorize_url(shop, app_credential.client_id, app_config.shopify_oauth_scopes, redirect_uri, state)
    return redirect(authorize_url, code=302)


@oauth_bp.get("/oauth/callback")
def callback():
    app_config = current_app.extensions["app_config"]
    params = request.args.to_dict()
    shop = params.get("shop", "").strip().lower()
    state = params.get("state", "")
    code = params.get("code", "")

    if not is_valid_shop_domain(shop):
        return _error("Invalid shop address.")
    if not code or not state:
        return _error("This install link is incomplete — ask for a fresh one.")

    state_row = db.session.get(OAuthState, state)
    if state_row is None or state_row.shop_domain != shop:
        # Covers both "never existed" and "already consumed" (consumed
        # below, right after this check passes) with the same message —
        # never distinguish the two to a caller.
        return _error("This install link has expired or was already used — ask for a fresh one.")
    if _state_expired(state_row, app_config):
        db.session.delete(state_row)
        db.session.commit()
        return _error("This install link has expired — ask for a fresh one.")

    app_credential = _app_credential_for_shop(shop)
    if app_credential is None:
        # Shouldn't happen (install already checked this), but the row
        # could theoretically be removed between install and callback.
        current_app.logger.error("oauth/callback: no ShopifyAppCredential for shop=%s", shop)
        return _error("This store isn't set up for install yet — please let us know and we'll sort it.", status=500)
    client_id = app_credential.client_id
    client_secret = decrypt(app_credential.encrypted_client_secret, app_config.credential_encryption_key)

    if not verify_callback_hmac(params, client_secret):
        current_app.logger.warning("oauth/callback: HMAC verification failed for shop=%s", shop)
        return _error("Could not verify this request.")

    # One-time use — delete the state now that it's verified, before doing
    # anything else, so a replayed callback can't reuse it.
    db.session.delete(state_row)
    db.session.commit()

    try:
        token_data = exchange_code_for_token(shop, client_id, client_secret, code)
    except ShopifyOAuthError as exc:
        current_app.logger.error("oauth/callback: token exchange failed for shop=%s: %s", shop, exc)
        return _error("Could not complete the connection with Shopify — please try installing again.", status=502)

    requested_scopes = {s.strip() for s in app_config.shopify_oauth_scopes.split(",") if s.strip()}
    granted_scopes = {s.strip() for s in token_data.get("scope", "").split(",") if s.strip()}
    if not requested_scopes.issubset(granted_scopes):
        current_app.logger.error("oauth/callback: scopes not fully granted for shop=%s (requested=%s, granted=%s)", shop, requested_scopes, granted_scopes)
        return _error("The requested permissions weren't fully granted — please try installing again and approve all requested access.")

    store_id = store_id_for_shop(shop)
    now = datetime.now(timezone.utc)
    encrypted_access = encrypt(token_data["access_token"], app_config.credential_encryption_key)
    encrypted_refresh = encrypt(token_data["refresh_token"], app_config.credential_encryption_key)
    access_expires_at = now + timedelta(seconds=token_data.get("expires_in", 0))
    refresh_expires_at = now + timedelta(seconds=token_data.get("refresh_token_expires_in", 0))

    credential_row = db.session.get(ShopifyOAuthCredential, store_id)
    if credential_row is None:
        credential_row = ShopifyOAuthCredential(store_id=store_id, shop_domain=shop)
        db.session.add(credential_row)
    credential_row.shop_domain = shop
    credential_row.encrypted_access_token = encrypted_access
    credential_row.encrypted_refresh_token = encrypted_refresh
    credential_row.scope = token_data.get("scope", "")
    credential_row.access_token_expires_at = access_expires_at
    credential_row.refresh_token_expires_at = refresh_expires_at
    credential_row.needs_reauth = False  # a fresh install always clears any prior reauth flag
    db.session.commit()  # token saved FIRST — this must land before anything that can run long

    # Provisioning (scrape the storefront + Claude knowledge-base draft)
    # runs out-of-band — see spawn_provisioning_job's docstring. A fresh
    # install has no stores/<id>.yaml yet, so store_name just falls back
    # to the shop domain; a re-opened install link for an already-
    # provisioned store (no network needed) gets the nicer drafted name.
    spawn_provisioning_job(store_id)
    store_name = shop
    try:
        store_config = load_store_config(store_id, stores_dir=app_config.stores_dir)
        store_name = store_config.store_name
    except Exception:
        pass  # the connected page still shows something sensible (the shop domain) either way

    return render_template("oauth_connected.html", store_name=store_name), 200


@oauth_bp.post("/webhooks/app-uninstalled")
def app_uninstalled():
    app_config = current_app.extensions["app_config"]
    raw_body = request.get_data()
    provided_hmac = request.headers.get("X-Shopify-Hmac-Sha256", "")

    shop = request.headers.get("X-Shopify-Shop-Domain", "").strip().lower()
    if not shop:
        payload = request.get_json(silent=True) or {}
        shop = (payload.get("domain") or "").strip().lower()

    if not shop:
        current_app.logger.warning("webhooks/app-uninstalled: no shop domain in request")
        return "", 200  # acknowledge anyway — Shopify retries on non-2xx

    # The shop domain above is UNTRUSTED until the HMAC below actually
    # verifies — it only selects which client's app secret to check
    # against, so a forged domain just fails verification with the wrong
    # secret rather than ever being trusted.
    app_credential = _app_credential_for_shop(shop)
    if app_credential is None:
        current_app.logger.warning("webhooks/app-uninstalled: no ShopifyAppCredential for shop=%s", shop)
        return "", 200
    client_secret = decrypt(app_credential.encrypted_client_secret, app_config.credential_encryption_key)

    if not verify_webhook_hmac(raw_body, provided_hmac, client_secret):
        current_app.logger.warning("webhooks/app-uninstalled: HMAC verification failed for shop=%s", shop)
        return "invalid signature", 401

    store_id = store_id_for_shop(shop)

    credential_row = db.session.get(ShopifyOAuthCredential, store_id)
    if credential_row is not None:
        db.session.delete(credential_row)
        db.session.commit()

    try:
        store_config = load_store_config(store_id, stores_dir=app_config.stores_dir)
    except Exception:
        current_app.logger.warning("webhooks/app-uninstalled: no store config found for store_id=%s (shop=%s)", store_id, shop)
        return "", 200

    yaml_path = app_config.stores_dir / f"{store_id}.yaml"
    raw = store_config.model_dump(exclude_none=False)
    raw["live_status"] = "inactive"
    with yaml_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, sort_keys=False, allow_unicode=True)

    return "", 200
