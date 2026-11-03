import pytest

from app.agent.order_lookup import (
    DEMO_ORDER_NOTICE,
    GENERIC_NOT_FOUND_MESSAGE,
    LIMIT_REACHED_MESSAGE,
    LookupBudget,
    handle_lookup_order,
    normalise_order_number,
)
from app.agent.tools import ToolContext
from app.config import AppConfig
from app.store_config import StoreConfig

DEMO_STORE_DATA = {
    "store_id": "demo-acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "admin_token_env": None,
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": [],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}

ORDER_ALLOWED_KEYS = {
    "order_number",
    "status",
    "financial_status",
    "fulfillment_status",
    "order_date",
    "shipments",
    "line_items",
}
SHIPMENT_ALLOWED_KEYS = {"carrier", "tracking_number", "tracking_url"}
LINE_ITEM_ALLOWED_KEYS = {"title", "variant", "quantity"}
FORBIDDEN_SUBSTRINGS = ("address", "phone", "payment", "card", "customer")


def _assert_no_pii(order: dict):
    assert set(order.keys()) <= ORDER_ALLOWED_KEYS
    for shipment in order.get("shipments", []):
        assert set(shipment.keys()) <= SHIPMENT_ALLOWED_KEYS
    for item in order.get("line_items", []):
        assert set(item.keys()) <= LINE_ITEM_ALLOWED_KEYS
    blob = str(order).lower()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in blob, f"found forbidden field {forbidden!r} in order payload: {order}"


def _demo_store():
    return StoreConfig.model_validate(DEMO_STORE_DATA)


def _ctx(store, app_config=None, lookup_budget=None):
    app_config = app_config or AppConfig()
    return ToolContext(store_config=store, app_config=app_config, lookup_budget=lookup_budget or LookupBudget())


@pytest.mark.parametrize("order_number", ["1001", "#1001", " 1001 ", " #1001 ", "#  1001"])
def test_normalise_order_number_variants(order_number):
    assert normalise_order_number(order_number) == "1001"


@pytest.mark.parametrize("order_number", ["1001", "EH-1001", "eh-1001", "#EH-1001", " EH-1001 "])
def test_normalise_order_number_with_configured_prefix(order_number):
    assert normalise_order_number(order_number, prefix="EH-") == "EH-1001"


@pytest.mark.parametrize("order_number", ["1001", "1001-UK", "1001-uk", "#1001-UK"])
def test_normalise_order_number_with_configured_suffix(order_number):
    assert normalise_order_number(order_number, suffix="-UK") == "1001-UK"


def test_normalise_order_number_with_prefix_and_suffix():
    assert normalise_order_number("EH-1001-UK", prefix="EH-", suffix="-UK") == "EH-1001-UK"
    assert normalise_order_number("1001", prefix="EH-", suffix="-UK") == "EH-1001-UK"


def test_handle_lookup_order_uses_store_configured_prefix(monkeypatch):
    live_data = {
        **DEMO_STORE_DATA,
        "store_id": "acme",
        "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME",
        "mode": "live",
        "order_number_prefix": "EH-",
    }
    store = StoreConfig.model_validate(live_data)
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test")

    captured = {}

    class _FakeShopifyClient:
        def __init__(self, shop_domain, api_version, access_token):
            pass

        def get_order(self, order_number, email):
            captured["order_number"] = order_number
            return None

    monkeypatch.setattr("app.shopify_client.ShopifyAdminClient", _FakeShopifyClient)
    handle_lookup_order(_ctx(store), {"order_number": "1001", "email": "demo@example.com"})
    assert captured["order_number"] == "EH-1001"


def test_normalise_order_number_non_string_returns_empty():
    assert normalise_order_number(None) == ""
    assert normalise_order_number(1001) == ""


@pytest.mark.parametrize("order_number", ["1001", "#1001", " 1001 "])
def test_demo_order_found_with_normalised_number(order_number):
    result, status = handle_lookup_order(
        _ctx(_demo_store()), {"order_number": order_number, "email": "demo@example.com"}
    )
    assert status == "ok"
    assert result["found"] is True
    _assert_no_pii(result["order"])


def test_demo_order_includes_demo_notice():
    result, status = handle_lookup_order(_ctx(_demo_store()), {"order_number": "1001", "email": "demo@example.com"})
    assert status == "ok"
    assert result["demo_notice"] == DEMO_ORDER_NOTICE


def test_demo_order_line_items_default_to_the_fashion_fixture():
    result, _ = handle_lookup_order(_ctx(_demo_store()), {"order_number": "1001", "email": "demo@example.com"})
    assert result["order"]["line_items"] == [{"title": "Merino Crew Jumper", "variant": "M", "quantity": 1}]


def test_demo_order_line_items_use_store_override_when_set():
    """A store selling something other than fashion needs its demo order to
    show a plausible line item, not the hardcoded fashion fixture."""
    override_data = {
        **DEMO_STORE_DATA,
        "demo_order_line_items": [{"title": "Fenland Stoneware Mug", "variant": "Moss glaze", "quantity": 2}],
    }
    store = StoreConfig.model_validate(override_data)
    result, _ = handle_lookup_order(_ctx(store), {"order_number": "1001", "email": "demo@example.com"})
    assert result["order"]["line_items"] == [{"title": "Fenland Stoneware Mug", "variant": "Moss glaze", "quantity": 2}]


def test_demo_order_has_no_real_tracking_url():
    result, status = handle_lookup_order(_ctx(_demo_store()), {"order_number": "1001", "email": "demo@example.com"})
    assert status == "ok"
    for shipment in result["order"]["shipments"]:
        assert shipment["tracking_url"] is None


def test_demo_order_email_case_and_whitespace_insensitive():
    result, status = handle_lookup_order(_ctx(_demo_store()), {"order_number": "1001", "email": "  DEMO@EXAMPLE.COM  "})
    assert status == "ok"
    assert result["found"] is True


def test_demo_wrong_email_explains_its_a_demo():
    result, status = handle_lookup_order(
        _ctx(_demo_store()), {"order_number": "1001", "email": "someone-else@example.com"}
    )
    assert status == "demo_mismatch"
    assert result["found"] is False
    assert "demo" in result["message"].lower()
    assert "1001" in result["message"]
    assert "demo@example.com" in result["message"]


def test_demo_wrong_order_number_explains_its_a_demo():
    result, status = handle_lookup_order(_ctx(_demo_store()), {"order_number": "9999", "email": "demo@example.com"})
    assert status == "demo_mismatch"
    assert "demo" in result["message"].lower()


def test_demo_mismatch_message_differs_from_generic_not_found():
    # Demo mode explains itself instead of using the security-motivated
    # generic message — there's no real order data to enumerate in demo mode.
    result, _ = handle_lookup_order(_ctx(_demo_store()), {"order_number": "9999", "email": "demo@example.com"})
    assert result["message"] != GENERIC_NOT_FOUND_MESSAGE


def test_live_mode_not_found_and_email_mismatch_produce_byte_identical_response(monkeypatch):
    """The enumeration-prevention guarantee applies to live stores (real
    order data) — demo mode's fixed example order has nothing to enumerate,
    so it explains itself instead (see test_demo_wrong_*_explains_its_a_demo).
    """
    live_data = {**DEMO_STORE_DATA, "store_id": "acme", "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME", "mode": "live"}
    store = StoreConfig.model_validate(live_data)
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test")

    class _FakeShopifyClient:
        def __init__(self, shop_domain, api_version, access_token):
            pass

        def get_order(self, order_number, email):
            return None  # both "no such order" and "wrong email" collapse to None by design

    monkeypatch.setattr("app.shopify_client.ShopifyAdminClient", _FakeShopifyClient)

    mismatch, mismatch_status = handle_lookup_order(_ctx(store), {"order_number": "1001", "email": "wrong@example.com"})
    not_found, not_found_status = handle_lookup_order(_ctx(store), {"order_number": "404404", "email": "real@example.com"})
    assert mismatch == not_found
    assert mismatch_status == not_found_status == "not_found"


def test_missing_fields_gets_a_different_message_than_not_found():
    budget = LookupBudget(session_remaining=5, ip_remaining=10)
    result, status = handle_lookup_order(_ctx(_demo_store(), lookup_budget=budget), {"order_number": "", "email": ""})
    assert status == "error"
    assert result["message"] != GENERIC_NOT_FOUND_MESSAGE


def test_every_call_consumes_budget_even_when_input_is_malformed():
    budget = LookupBudget(session_remaining=1, ip_remaining=10)
    ctx = _ctx(_demo_store(), lookup_budget=budget)
    handle_lookup_order(ctx, {"order_number": "", "email": ""})
    assert budget.session_remaining == 0
    result, status = handle_lookup_order(ctx, {"order_number": "1001", "email": "demo@example.com"})
    assert status == "blocked_session_limit"
    assert result["found"] is False


def test_session_budget_exhausted_after_five_calls():
    budget = LookupBudget(session_remaining=5, ip_remaining=999)
    ctx = _ctx(_demo_store(), lookup_budget=budget)
    for _ in range(5):
        _, status = handle_lookup_order(ctx, {"order_number": "9999", "email": "nope@example.com"})
        assert status != "blocked_session_limit"
    result, status = handle_lookup_order(ctx, {"order_number": "1001", "email": "demo@example.com"})
    assert status == "blocked_session_limit"
    assert result["found"] is False
    assert result["message"] == LIMIT_REACHED_MESSAGE


def test_ip_budget_exhausted_blocks_even_with_session_budget_left():
    budget = LookupBudget(session_remaining=999, ip_remaining=0)
    ctx = _ctx(_demo_store(), lookup_budget=budget)
    result, status = handle_lookup_order(ctx, {"order_number": "1001", "email": "demo@example.com"})
    assert status == "blocked_ip_limit"
    assert result["message"] == LIMIT_REACHED_MESSAGE


def test_live_mode_lookup_returns_no_pii(monkeypatch):
    live_data = {**DEMO_STORE_DATA, "store_id": "acme", "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME", "mode": "live"}
    store = StoreConfig.model_validate(live_data)

    class _FakeShopifyClient:
        def __init__(self, shop_domain, api_version, access_token):
            pass

        def get_order(self, order_number, email):
            if order_number == "2002" and email == "jane@example.com":
                return {
                    "order_number": "2002",
                    "status": "open",
                    "financial_status": "paid",
                    "fulfillment_status": "fulfilled",
                    "order_date": "2026-09-20",
                    "shipments": [{"carrier": "Evri", "tracking_number": "EV1", "tracking_url": "https://track"}],
                    "line_items": [{"title": "Wool Overcoat", "variant": "UK 12", "quantity": 1}],
                }
            return None

    monkeypatch.setattr("app.shopify_client.ShopifyAdminClient", _FakeShopifyClient)
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test")

    app_config = AppConfig()
    app_config.shopify_api_version = "2025-01"
    result, status = handle_lookup_order(
        _ctx(store, app_config=app_config), {"order_number": "#2002", "email": " Jane@Example.com "}
    )
    assert status == "ok"
    _assert_no_pii(result["order"])
