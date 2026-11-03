"""Authorization code grant (OAuth install flow) for client stores — see
docs/OAUTH_PLAN.md. Pure logic (domain validation, HMAC verification, the
two token HTTP calls), deliberately kept separate from app/routes/oauth.py
so it's testable without a Flask request context, mirroring how
app/shopify_client.py's token-fetching is separate from any route.

Distinct from app/shopify_client.py's client-credentials grant: that only
works when the app and the store share a Shopify organization (fine for
emberhome, our own store). This is for an actual client's store, in its
own, separate organization — the install link flow, via a custom
distribution app in the Dev Dashboard.
"""

import base64
import hashlib
import hmac
import re
from urllib.parse import urlencode

import requests

SHOP_DOMAIN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")


class ShopifyOAuthError(Exception):
    """Raised for conditions the OAuth flow should reject and stop at —
    callers (app/routes/oauth.py) decide how to report that (always a
    generic error page to the client; details only in server logs).
    """


class ShopifyTokenRefreshError(Exception):
    """Raised by refresh_access_token on a non-2xx response. status_code
    lets the caller (app.shopify_client._OAuthTokenProvider) distinguish
    a dead refresh token (401 — stop retrying, mark needs_reauth) from a
    transient failure (5xx/429 — safe to retry with backoff).
    """

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def is_valid_shop_domain(shop: str) -> bool:
    """True only for a bare `xxx.myshopify.com` domain — rejects e.g.
    `realshop.myshopify.com.evil.example` (a suffix-match bypass) or a
    custom domain (OAuth always happens against the myshopify.com domain,
    regardless of what custom domain a store's storefront uses).
    """
    return bool(shop and SHOP_DOMAIN_RE.match(shop))


def build_authorize_url(shop: str, client_id: str, scopes: str, redirect_uri: str, state: str) -> str:
    params = {"client_id": client_id, "scope": scopes, "redirect_uri": redirect_uri, "state": state}
    return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"


def verify_callback_hmac(params: dict, client_secret: str) -> bool:
    """Shopify's documented callback-verification algorithm: drop `hmac`
    (and `signature`, an older/deprecated equivalent some requests still
    carry) from the query params, sort what's left by key, join as
    `key=value` pairs with `&`, HMAC-SHA256 with the app's client secret,
    and compare to the provided hex digest with a timing-safe comparison.
    """
    provided = params.get("hmac", "")
    if not provided:
        return False
    filtered = {k: v for k, v in params.items() if k not in ("hmac", "signature")}
    message = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    computed = hmac.new(client_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, provided)


def verify_webhook_hmac(raw_body: bytes, provided_hmac_b64: str, client_secret: str) -> bool:
    """Shopify signs a webhook body with base64(HMAC-SHA256(body, client_secret))
    in the X-Shopify-Hmac-Sha256 header — same client secret as OAuth
    (no separate webhook signing secret for a webhook subscribed via the
    app's own Configuration, as opposed to one created via the REST/GraphQL
    webhook API with its own secret).
    """
    if not provided_hmac_b64:
        return False
    digest = hmac.new(client_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed_b64 = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed_b64, provided_hmac_b64)


def exchange_code_for_token(shop: str, client_id: str, client_secret: str, code: str, timeout: float = 10.0) -> dict:
    """POST https://{shop}/admin/oauth/access_token — exchanges the
    one-time `code` from the callback for an access + refresh token pair.
    `expiring: "1"` is required to get that shape; without it Shopify
    issues the old-style non-expiring token instead.
    Response: {"access_token", "scope", "expires_in", "refresh_token",
    "refresh_token_expires_in"}.
    """
    url = f"https://{shop}/admin/oauth/access_token"
    try:
        response = requests.post(
            url,
            json={"client_id": client_id, "client_secret": client_secret, "code": code, "expiring": "1"},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ShopifyOAuthError(f"token exchange failed: {exc}") from exc
    return response.json()


def refresh_access_token(shop: str, client_id: str, client_secret: str, refresh_token: str, timeout: float = 10.0) -> dict:
    """POST https://{shop}/admin/oauth/access_token with grant_type
    refresh_token — same response shape as exchange_code_for_token. Raises
    ShopifyTokenRefreshError (carrying the status code) on any non-2xx
    response, rather than swallowing it, so the caller can tell a dead
    refresh token (401) from a transient failure (5xx/429).
    """
    url = f"https://{shop}/admin/oauth/access_token"
    try:
        response = requests.post(
            url,
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise ShopifyOAuthError(f"token refresh request failed: {exc}") from exc
    if not response.ok:
        raise ShopifyTokenRefreshError(response.status_code, f"token refresh failed: HTTP {response.status_code}")
    return response.json()
