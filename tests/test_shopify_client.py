from app.shopify_client import ShopifyAdminClient

ORDER_ALLOWED_KEYS = {
    "order_number",
    "status",
    "financial_status",
    "fulfillment_status",
    "order_date",
    "shipments",
    "line_items",
}
FORBIDDEN_SUBSTRINGS = ("address", "phone", "payment", "card", "customer")


def _client():
    return ShopifyAdminClient(shop_domain="acme.myshopify.com", api_version="2025-01", access_token="tok")


def _node(email="jane@example.com", **overrides):
    base = {
        "name": "#2002",
        "email": email,
        "createdAt": "2026-09-20T10:00:00Z",
        "cancelledAt": None,
        "closedAt": None,
        "displayFinancialStatus": "PAID",
        "displayFulfillmentStatus": "FULFILLED",
        "lineItems": {"edges": [{"node": {"title": "Wool Overcoat", "quantity": 1, "variant": {"title": "UK 12"}}}]},
        "fulfillments": [{"trackingInfo": [{"number": "RM1", "url": "https://track", "company": "Royal Mail"}]}],
    }
    base.update(overrides)
    return base


def test_get_order_returns_none_when_no_orders_match(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": []}})
    assert client.get_order("2002", "jane@example.com") is None


def test_get_order_returns_none_on_email_mismatch(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node(email="other@example.com")}]}})
    assert client.get_order("2002", "jane@example.com") is None


def test_get_order_matches_case_and_whitespace_insensitively(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node(email="Jane@Example.com")}]}})
    order = client.get_order("2002", "jane@example.com")
    assert order is not None
    assert order["order_number"] == "2002"


def test_get_order_shape_has_no_pii(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node()}]}})
    order = client.get_order("2002", "jane@example.com")

    assert set(order.keys()) <= ORDER_ALLOWED_KEYS
    for shipment in order["shipments"]:
        assert set(shipment.keys()) == {"carrier", "tracking_number", "tracking_url"}
    for item in order["line_items"]:
        assert set(item.keys()) == {"title", "variant", "quantity"}

    blob = str(order).lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in blob, f"found forbidden field {forbidden!r} in order payload: {order}"


def test_get_order_cancelled_status(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node(cancelledAt="2026-09-21T00:00:00Z")}]}}
    )
    order = client.get_order("2002", "jane@example.com")
    assert order["status"] == "cancelled"


def test_get_order_closed_status(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node(closedAt="2026-09-21T00:00:00Z")}]}}
    )
    order = client.get_order("2002", "jane@example.com")
    assert order["status"] == "closed"


def test_get_order_open_status_by_default(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node()}]}})
    order = client.get_order("2002", "jane@example.com")
    assert order["status"] == "open"


def test_get_order_no_fulfillments_yields_empty_shipments(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"orders": {"edges": [{"node": _node(fulfillments=[])}]}})
    order = client.get_order("2002", "jane@example.com")
    assert order["shipments"] == []


def test_get_order_query_uses_given_order_number_string(monkeypatch):
    client = _client()
    captured = {}

    def fake_graphql(query, variables):
        captured.update(variables)
        return {"orders": {"edges": []}}

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    client.get_order("2002", "jane@example.com")
    assert captured["query"] == "name:2002"


def _product_edge(**overrides):
    node = {
        "title": "Wool Overcoat",
        "description": "Falls to mid-calf, 80% wool.",
        "onlineStoreUrl": "https://acme.co.uk/products/wool-overcoat",
        "variants": {"edges": [{"node": {"title": "UK 12", "availableForSale": True, "price": "189.00"}}]},
    }
    node.update(overrides)
    return {"node": node}


def test_search_products_includes_description(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql", lambda q, v: {"products": {"edges": [_product_edge()]}})
    results = client.search_products("overcoat")
    assert results[0].description == "Falls to mid-calf, 80% wool."
    assert results[0].title == "Wool Overcoat"
    assert results[0].price == "£189.00"
    assert results[0].available is True


def test_search_products_missing_description_defaults_to_empty_string(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client, "_graphql", lambda q, v: {"products": {"edges": [_product_edge(description=None)]}}
    )
    results = client.search_products("overcoat")
    assert results[0].description == ""


# ---------- get_diagnostics ----------


def _diagnostics_raw(orders=None, scopes=("read_orders", "read_products"), errors=None):
    if orders is None:
        orders = [{"name": "#2003", "createdAt": "2026-09-22T10:00:00Z", "email": "jane@example.com"}]
    data = {}
    if orders is not None:
        data["orders"] = {"edges": [{"node": o} for o in orders]}
    if scopes is not None:
        data["currentAppInstallation"] = {"accessScopes": [{"handle": s} for s in scopes]}
    raw = {"data": data}
    if errors is not None:
        raw["errors"] = errors
    return raw


def test_get_diagnostics_happy_path(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: _diagnostics_raw())
    result = client.get_diagnostics()
    assert result["scopes"] == ["read_orders", "read_products"]
    assert result["errors"] == []
    assert result["orders"] == [{"name": "#2003", "created_at": "2026-09-22T10:00:00Z", "email_present": True}]


def test_get_diagnostics_never_includes_email_value(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: _diagnostics_raw())
    result = client.get_diagnostics()
    assert "jane@example.com" not in str(result)


def test_get_diagnostics_null_email_reported_as_false(monkeypatch):
    client = _client()
    orders = [{"name": "#2004", "createdAt": "2026-09-22T10:00:00Z", "email": None}]
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: _diagnostics_raw(orders=orders))
    result = client.get_diagnostics()
    assert result["orders"][0]["email_present"] is False


def test_get_diagnostics_passes_through_raw_errors_without_raising(monkeypatch):
    client = _client()
    errors = [{"message": "This action requires merchant approval for protected customer data"}]
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: _diagnostics_raw(errors=errors))
    result = client.get_diagnostics()
    assert result["errors"] == errors


def test_get_diagnostics_no_orders(monkeypatch):
    client = _client()
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: _diagnostics_raw(orders=[]))
    result = client.get_diagnostics()
    assert result["orders"] == []


# ---------- get_shop_domains ----------


def test_get_shop_domains_happy_path(monkeypatch):
    client = _client()
    monkeypatch.setattr(
        client,
        "_graphql_raw",
        lambda q, v: {
            "data": {
                "shop": {
                    "primaryDomain": {"url": "https://acme.co.uk", "host": "acme.co.uk"},
                    "myshopifyDomain": "acme-fashion.myshopify.com",
                }
            }
        },
    )
    result = client.get_shop_domains()
    assert result == {"primary_host": "acme.co.uk", "myshopify_domain": "acme-fashion.myshopify.com"}


def test_get_shop_domains_raises_catalog_error_on_graphql_error(monkeypatch):
    import pytest

    from app.catalog import CatalogError

    client = _client()
    monkeypatch.setattr(client, "_graphql_raw", lambda q, v: {"errors": [{"message": "access denied"}]})
    with pytest.raises(CatalogError):
        client.get_shop_domains()


# ---------- build_oauth_client ----------


def test_build_oauth_client_uses_store_id_and_shop_domain_directly():
    """Unlike build_client_for_store, this must work with no StoreConfig
    at all — app.live_onboarding.provision_live_store calls it before any
    stores/<store_id>.yaml exists.
    """
    from app.config import AppConfig
    from app.shopify_client import build_oauth_client

    app_config = AppConfig()
    app_config.shopify_api_version = "2025-01"
    client = build_oauth_client("acme", "acme.myshopify.com", app_config)
    assert client._url == "https://acme.myshopify.com/admin/api/2025-01/graphql.json"
