import time
from datetime import datetime, timedelta, timezone

import requests

from app.catalog import CatalogError, ProductResult, ProductVariant, format_price_summary, parse_price
from app.credential_encryption import encrypt
from app.credential_encryption import decrypt as decrypt_credential
from app.shopify_oauth import ShopifyOAuthError, ShopifyTokenRefreshError, refresh_access_token

# Refresh a client-credentials token this many seconds before its actual
# expiry, so a request that starts just before expiry doesn't race it.
_TOKEN_REFRESH_MARGIN_SECONDS = 300


def fetch_client_credentials_token(shop_domain: str, client_id: str, client_secret: str, timeout: float = 10.0) -> dict:
    """POST the Shopify client credentials grant and return the raw JSON response.

    See https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant
    Response shape: {"access_token": str, "scope": str, "expires_in": 86399}.
    Only works when the app (client_id/client_secret, from the Dev Dashboard)
    and the store belong to the same Shopify organization.
    """
    url = f"https://{shop_domain}/admin/oauth/access_token"
    try:
        response = requests.post(
            url,
            data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise CatalogError(f"Shopify token request failed: {exc}") from exc
    return response.json()


class _StaticTokenProvider:
    """Legacy custom-app flow: a long-lived token that doesn't expire."""

    def __init__(self, token: str):
        self._token = token

    def get_token(self) -> str:
        return self._token


class _ClientCredentialsTokenProvider:
    """Client credentials grant: fetches a token good for 24h and auto-refreshes it.

    There's no refresh token — refreshing just means requesting a new token
    with the same client_id/client_secret.
    """

    def __init__(self, shop_domain: str, client_id: str, client_secret: str, timeout: float = 10.0):
        self._shop_domain = shop_domain
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout
        self._token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        if self._token is None or time.time() >= self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
            self._refresh()
        return self._token

    def _refresh(self) -> None:
        data = fetch_client_credentials_token(
            self._shop_domain, self._client_id, self._client_secret, timeout=self._timeout
        )
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 0)


class _OAuthTokenProvider:
    """Authorization code grant (see app.shopify_oauth, docs/OAUTH_PLAN.md):
    the DB-backed, per-store counterpart to _ClientCredentialsTokenProvider.
    Unlike that one, this grant DOES have a refresh token, and the
    credential lives in the database (app.models.ShopifyOAuthCredential),
    encrypted at rest — not in memory alone — since it must survive
    process restarts and be correct even if multiple gunicorn workers each
    build their own client for the same store.

    A custom-distribution app only installs on the ONE store it was
    generated for, so there's a separate app (client_id + secret) per
    client — looked up from app.models.ShopifyAppCredential by store_id
    at refresh time, rather than passed in once at construction, since
    it's just as DB-backed/dynamic as the token itself.
    """

    def __init__(self, store_id: str, shop_domain: str, encryption_key: str, timeout: float = 10.0):
        self._store_id = store_id
        self._shop_domain = shop_domain
        self._encryption_key = encryption_key
        self._timeout = timeout

    def get_token(self) -> str:
        from app.extensions import db
        from app.models import ShopifyAppCredential, ShopifyOAuthCredential

        row = db.session.get(ShopifyOAuthCredential, self._store_id)
        if row is None:
            raise CatalogError(f"no Shopify OAuth credential stored for store {self._store_id!r} — needs to (re)install")
        if row.needs_reauth:
            raise CatalogError(f"store {self._store_id!r} needs reauthorization — send a fresh install link")

        if not row.encrypted_refresh_token:
            # No refresh token stored (e.g. a row created before this app
            # requested expiring=1 tokens, or imported by hand) — nothing
            # to refresh with, and no real expiry to check against either.
            # Treat it as a static, non-expiring token: if it's ever
            # actually dead, the real Shopify API call will fail with its
            # own 401/403 rather than us crashing here calling the refresh
            # endpoint with no refresh token to send.
            return decrypt_credential(row.encrypted_access_token, self._encryption_key)

        now = datetime.now(timezone.utc)
        expires_at = row.access_token_expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now < expires_at - timedelta(seconds=_TOKEN_REFRESH_MARGIN_SECONDS):
            return decrypt_credential(row.encrypted_access_token, self._encryption_key)

        app_credential = db.session.get(ShopifyAppCredential, self._store_id)
        if app_credential is None:
            raise CatalogError(f"no Shopify app credential stored for store {self._store_id!r} — run scripts/add_client_app.py")
        client_id = app_credential.client_id
        client_secret = decrypt_credential(app_credential.encrypted_client_secret, self._encryption_key)

        refresh_token = decrypt_credential(row.encrypted_refresh_token, self._encryption_key)
        try:
            data = refresh_access_token(self._shop_domain, client_id, client_secret, refresh_token, timeout=self._timeout)
        except ShopifyTokenRefreshError as exc:
            if exc.status_code == 401:
                # The refresh token itself is dead (expired, revoked, or
                # the app was uninstalled) — stop retrying in a loop;
                # only a fresh install fixes this.
                row.needs_reauth = True
                db.session.commit()
            raise CatalogError(f"failed to refresh Shopify token for store {self._store_id!r}: {exc}") from exc
        except ShopifyOAuthError as exc:
            # Network error / non-refresh-specific failure — safe to let
            # the caller retry later without marking needs_reauth.
            raise CatalogError(f"failed to refresh Shopify token for store {self._store_id!r}: {exc}") from exc

        new_access_token = data["access_token"]
        row.encrypted_access_token = encrypt(new_access_token, self._encryption_key)
        row.access_token_expires_at = now + timedelta(seconds=data.get("expires_in", 0))
        # Shopify may or may not rotate the refresh token on each refresh —
        # only overwrite it when the response actually includes a new one.
        new_refresh_token = data.get("refresh_token")
        if new_refresh_token:
            row.encrypted_refresh_token = encrypt(new_refresh_token, self._encryption_key)
            row.refresh_token_expires_at = now + timedelta(seconds=data.get("refresh_token_expires_in", 0))
        db.session.commit()
        return new_access_token


_SEARCH_PRODUCTS_QUERY = """
query SearchProducts($query: String!, $first: Int!) {
  products(first: $first, query: $query) {
    edges {
      node {
        title
        description
        onlineStoreUrl
        variants(first: 10) {
          edges {
            node {
              title
              availableForSale
              price
            }
          }
        }
      }
    }
  }
}
"""

_DIAGNOSTICS_QUERY = """
query DiagnoseStore {
  currentAppInstallation {
    accessScopes {
      handle
    }
  }
  orders(first: 3, sortKey: CREATED_AT, reverse: true) {
    edges {
      node {
        name
        createdAt
        email
      }
    }
  }
}
"""

_SHOP_DOMAINS_QUERY = """
query ShopDomains {
  shop {
    primaryDomain {
      url
      host
    }
    myshopifyDomain
  }
}
"""

_GET_ORDER_QUERY = """
query GetOrder($query: String!) {
  orders(first: 1, query: $query) {
    edges {
      node {
        name
        email
        createdAt
        cancelledAt
        closedAt
        displayFinancialStatus
        displayFulfillmentStatus
        lineItems(first: 20) {
          edges {
            node {
              title
              quantity
              variant {
                title
              }
            }
          }
        }
        fulfillments(first: 5) {
          trackingInfo {
            number
            url
            company
          }
        }
      }
    }
  }
}
"""


class ShopifyAdminClient:
    """Thin wrapper around the Shopify Admin GraphQL API for a single store.

    Accepts a static access_token (legacy custom-app flow), a client_id +
    client_secret pair (client credentials grant, Dev Dashboard apps — same
    org only), or a pre-built token_provider (the OAuth authorization code
    grant's _OAuthTokenProvider, for a real client in its own org — see
    build_client_for_store) — in the latter two cases the access token is
    fetched lazily and refreshed automatically before it expires.
    """

    def __init__(
        self,
        shop_domain: str,
        api_version: str,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_provider=None,
        timeout: float = 10.0,
    ):
        if not api_version:
            raise CatalogError("SHOPIFY_API_VERSION is not set")
        if token_provider is not None:
            self._token_provider = token_provider
        elif access_token:
            self._token_provider = _StaticTokenProvider(access_token)
        elif client_id and client_secret:
            self._token_provider = _ClientCredentialsTokenProvider(shop_domain, client_id, client_secret, timeout)
        else:
            raise CatalogError("ShopifyAdminClient requires access_token, client_id + client_secret, or token_provider")
        self._url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self._timeout = timeout

    def _graphql_raw(self, query: str, variables: dict) -> dict:
        """POST a GraphQL request and return the full response body
        (both "data" and "errors", if present) without raising on
        "errors" alone — Shopify returns HTTP 200 with partial data PLUS
        an "errors" entry for fields redacted due to protected-customer-data
        access rules, which isn't a hard failure. Only _graphql (below)
        treats any "errors" entry as fatal; callers that need to tell
        redaction apart from a real failure should call this instead.
        """
        headers = {
            "X-Shopify-Access-Token": self._token_provider.get_token(),
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self._url,
                json={"query": query, "variables": variables},
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CatalogError(f"Shopify API request failed: {exc}") from exc
        return response.json()

    def _graphql(self, query: str, variables: dict) -> dict:
        data = self._graphql_raw(query, variables)
        if "errors" in data:
            raise CatalogError(f"Shopify API error: {data['errors']}")
        return data["data"]

    def get_order(self, order_number: str, email: str) -> dict | None:
        """Fetch an order by number and return it only if `email` matches.

        Returns None both when no order matches `order_number` and when the
        order's email doesn't match — callers must not distinguish between
        these to avoid leaking which order numbers exist.
        """
        data = self._graphql(_GET_ORDER_QUERY, {"query": f"name:{order_number}"})
        edges = data["orders"]["edges"]
        if not edges:
            return None

        node = edges[0]["node"]
        order_email = (node.get("email") or "").strip().lower()
        if order_email != email:
            return None

        if node.get("cancelledAt"):
            status = "cancelled"
        elif node.get("closedAt"):
            status = "closed"
        else:
            status = "open"

        line_items = [
            {
                "title": edge["node"]["title"],
                "variant": (edge["node"].get("variant") or {}).get("title"),
                "quantity": edge["node"]["quantity"],
            }
            for edge in node["lineItems"]["edges"]
        ]

        shipments = [
            {
                "carrier": tracking.get("company"),
                "tracking_number": tracking.get("number"),
                "tracking_url": tracking.get("url"),
            }
            for fulfillment in node.get("fulfillments", [])
            for tracking in fulfillment.get("trackingInfo", [])
        ]

        return {
            "order_number": node["name"].lstrip("#"),
            "status": status,
            "financial_status": node.get("displayFinancialStatus"),
            "fulfillment_status": node.get("displayFulfillmentStatus"),
            "order_date": node.get("createdAt"),
            "shipments": shipments,
            "line_items": line_items,
        }

    def get_diagnostics(self) -> dict:
        """For scripts/diagnose_store.py: the 3 most recently created
        orders (name, created date, and whether email is non-null — never
        the email value itself) plus the live granted access scopes for
        this store's current token, and any raw GraphQL errors (e.g. a
        protected-customer-data redaction notice). Never raises on an
        "errors" entry alone — only a request/connection failure raises
        CatalogError; redaction is reported back for the caller to explain.
        """
        raw = self._graphql_raw(_DIAGNOSTICS_QUERY, {})
        data = raw.get("data") or {}

        orders = [
            {
                "name": edge["node"]["name"],
                "created_at": edge["node"]["createdAt"],
                "email_present": edge["node"].get("email") is not None,
            }
            for edge in (data.get("orders") or {}).get("edges", [])
        ]
        scopes = [
            s["handle"] for s in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])
        ]
        return {
            "orders": orders,
            "scopes": scopes,
            "errors": raw.get("errors") or [],
        }

    def get_shop_domains(self) -> dict:
        """{"primary_host": str, "myshopify_domain": str} — Shopify's own
        record of this shop's domains, used to auto-populate
        allowed_origins (see app.origin_discovery, app.live_onboarding,
        scripts/refresh_store_origins.py) rather than setting it by hand.
        Raises CatalogError on a request/GraphQL failure — including an
        access-scope issue, which Shopify also reports as a GraphQL error
        rather than a distinct status code, so it surfaces here the same
        way rather than being silently swallowed.
        """
        data = self._graphql(_SHOP_DOMAINS_QUERY, {})
        shop = data["shop"]
        return {
            "primary_host": shop["primaryDomain"]["host"],
            "myshopify_domain": shop["myshopifyDomain"],
        }

    def search_products(self, query: str, limit: int = 5) -> list[ProductResult]:
        data = self._graphql(_SEARCH_PRODUCTS_QUERY, {"query": query, "first": limit})
        results = []
        for edge in data["products"]["edges"]:
            node = edge["node"]
            variant_edges = node["variants"]["edges"]
            variants = []
            prices = []
            for v in variant_edges:
                vn = v["node"]
                price_raw = vn.get("price")
                variants.append(
                    ProductVariant(
                        title=vn.get("title", ""),
                        price=f"£{price_raw}" if price_raw else "",
                        available=bool(vn.get("availableForSale")),
                    )
                )
                parsed = parse_price(price_raw)
                if parsed is not None:
                    prices.append(parsed)
            available = any(v.available for v in variants)
            results.append(
                ProductResult(
                    title=node["title"],
                    price=format_price_summary(prices),
                    variants=variants,
                    available=available,
                    url=node.get("onlineStoreUrl") or "",
                    description=node.get("description") or "",
                )
            )
        return results


def build_client_for_store(store_config, app_config) -> ShopifyAdminClient:
    """Build a ShopifyAdminClient for a live-mode store, using whichever
    credential mechanism the store config declares: a static admin_token_env
    (legacy custom-app token), client_id_env/client_secret_env (client
    credentials grant — same Shopify org only), or — when neither is set —
    a row in the OAuth credentials table (an actual client, onboarded via
    the install link; see app/routes/oauth.py), looked up by store_id.
    """
    if store_config.admin_token_env:
        token = store_config.get_admin_token()
        return ShopifyAdminClient(
            shop_domain=store_config.shop_domain,
            api_version=app_config.shopify_api_version,
            access_token=token,
        )
    if store_config.uses_client_credentials():
        client_id, client_secret = store_config.get_shopify_client_credentials()
        return ShopifyAdminClient(
            shop_domain=store_config.shop_domain,
            api_version=app_config.shopify_api_version,
            client_id=client_id,
            client_secret=client_secret,
        )

    from app.extensions import db
    from app.models import ShopifyOAuthCredential

    if db.session.get(ShopifyOAuthCredential, store_config.store_id) is None:
        raise CatalogError(f"store {store_config.store_id!r} has no Shopify credentials configured")

    token_provider = _OAuthTokenProvider(
        store_id=store_config.store_id,
        shop_domain=store_config.shop_domain,
        encryption_key=app_config.credential_encryption_key,
    )
    return ShopifyAdminClient(
        shop_domain=store_config.shop_domain,
        api_version=app_config.shopify_api_version,
        token_provider=token_provider,
    )


def build_oauth_client(store_id: str, shop_domain: str, app_config) -> ShopifyAdminClient:
    """Build a ShopifyAdminClient straight from the OAuth credentials
    table, without a StoreConfig/yaml at all — used by
    app.live_onboarding.provision_live_store, which runs BEFORE
    stores/<store_id>.yaml exists (the OAuth callback already saved the
    token to the DB first; provisioning only has store_id + shop_domain
    at that point, both already known). build_client_for_store above is
    for everything AFTER a yaml exists.
    """
    token_provider = _OAuthTokenProvider(
        store_id=store_id,
        shop_domain=shop_domain,
        encryption_key=app_config.credential_encryption_key,
    )
    return ShopifyAdminClient(
        shop_domain=shop_domain,
        api_version=app_config.shopify_api_version,
        token_provider=token_provider,
    )
