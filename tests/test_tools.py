import json

import pytest

from app.agent.tools import ToolContext, handle_get_policy, handle_search_products
from app.config import AppConfig
from app.store_config import StoreConfig

BASE_STORE = {
    "store_id": "acme",
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
    "forbidden": [],
    "mode": "demo",
    # A normal scraped sales demo has this set (scripts/create_demo.py always
    # sets it) — matters for the staleness tests below, since a store with
    # no demo_created_at is treated as a permanent hand-authored fixture
    # that's never flagged stale (see handle_search_products).
    "demo_created_at": "2026-01-01T00:00:00+00:00",
}


def _store(**kb_overrides):
    knowledge_base = {
        "shipping": "Royal Mail, 2-3 days.",
        "returns": "30 days, unworn.",
        "sizing": "True to size.",
        "brand_claims": "All candles are made with pet-safe, phthalate-free fragrance oils.",
        "faq": [{"question": "Do you ship to Ireland?", "answer": "Yes, via Evri."}],
        **kb_overrides,
    }
    return StoreConfig.model_validate({**BASE_STORE, "knowledge_base": knowledge_base})


@pytest.fixture
def app_config(tmp_path):
    config = AppConfig()
    config.stores_dir = tmp_path
    return config


def _ctx(app_config, store):
    return ToolContext(store_config=store, app_config=app_config)


def test_get_policy_matches_shipping_keyword(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "how long does delivery take"})
    assert status == "ok"
    assert "Royal Mail" in result["text"]


def test_get_policy_matches_returns_keyword(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "can I get a refund"})
    assert status == "ok"
    assert "30 days" in result["text"]


def test_get_policy_matches_sizing_keyword(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "does this fit true to size"})
    assert status == "ok"
    assert "True to size" in result["text"]


def test_get_policy_matches_washing_keyword_to_sizing_field(app_config):
    """Regression: a customer asked "how should I wash my jumper?" and got
    "no washing guide to hand" — garment-care/washing questions must route
    to the sizing field (which now also carries care instructions) rather
    than falling through to not_found.
    """
    store = _store(sizing="True to size. Hand wash only, lay flat to dry.")
    result, status = handle_get_policy(_ctx(app_config, store), {"topic": "how should I wash my jumper"})
    assert status == "ok"
    assert "Hand wash only" in result["text"]


def test_get_policy_matches_brand_claims_keyword(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "are your candles safe to burn around my dog"})
    assert status == "ok"
    assert "pet-safe" in result["text"]


def test_get_policy_matches_faq_by_similarity(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "Do you ship to Ireland?"})
    assert status == "ok"
    assert "Evri" in result["text"]


def test_get_policy_unknown_topic_not_found(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "what is the meaning of life"})
    assert status == "not_found"
    assert result["found"] is False


def test_get_policy_empty_topic_is_error(app_config):
    result, status = handle_get_policy(_ctx(app_config, _store()), {"topic": "  "})
    assert status == "error"


def test_search_products_uses_static_catalog(app_config):
    products_path = app_config.stores_dir / "acme.products.json"
    products_path.write_text(json.dumps([{"title": "Wool Overcoat", "price": "£189.00", "variants": [], "available": True, "url": "u"}]))
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": "overcoat"})
    assert status == "ok"
    assert result["products"][0]["title"] == "Wool Overcoat"


def test_search_products_passes_through_bundle_variant_title_and_exact_price(app_config):
    """B2 spec: a bundle/multi-buy product's price must reach the model
    exactly as the catalog states it, together with the variant/bundle
    title ("Bundle of 5 — £20.00") — never a per-unit price computed by
    dividing the bundle total, which isn't this layer's job at all (see
    the system prompt's "quote prices exactly as listed" rule for the
    model-side half of this).
    """
    products_path = app_config.stores_dir / "acme.products.json"
    products_path.write_text(
        json.dumps(
            [
                {
                    "title": "Fresh & Clean Clamshell Bundle",
                    "variants": [{"title": "Bundle of 5", "price": "20.00", "available": True}],
                    "url": "u",
                }
            ]
        )
    )
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": "bundle"})
    assert status == "ok"
    product = result["products"][0]
    assert product["price"] == "£20.00"
    assert product["variants"][0]["title"] == "Bundle of 5"
    assert product["variants"][0]["price"] == "20.00"
    # Never divided by the bundle size — this layer passes through the
    # catalog's own price, it doesn't compute a per-unit figure.
    assert "4.00" not in json.dumps(product)


def test_search_products_empty_query_is_error(app_config):
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": ""})
    assert status == "error"


def test_search_products_no_results(app_config):
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": "nonexistent"})
    assert status == "ok"
    assert result["products"] == []


def test_search_products_flags_stale_data_with_no_refresh_recorded(app_config):
    products_path = app_config.stores_dir / "acme.products.json"
    products_path.write_text(json.dumps([{"title": "Wool Overcoat", "price": "£189.00", "variants": [], "available": True, "url": "u"}]))
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": "overcoat"})
    assert status == "ok"
    assert result["stock_data_may_be_stale"] is True


def test_search_products_not_flagged_stale_after_recent_refresh(app_config):
    from app.catalog import write_fetched_at

    products_path = app_config.stores_dir / "acme.products.json"
    products_path.write_text(json.dumps([{"title": "Wool Overcoat", "price": "£189.00", "variants": [], "available": True, "url": "u"}]))
    write_fetched_at(products_path)
    result, status = handle_search_products(_ctx(app_config, _store()), {"query": "overcoat"})
    assert status == "ok"
    assert result["stock_data_may_be_stale"] is False


def test_search_products_never_flagged_stale_for_permanent_fixture_with_no_demo_created_at(app_config):
    """Regression: a permanent, hand-authored demo (e.g. site-demo — no
    demo_created_at, unlike a real scraped sales demo) isn't on the
    scripts/refresh_demo_catalogs.py refresh lifecycle at all, so it must
    never be flagged stale regardless of file age — otherwise a permanent
    public demo would start hedging on stock certainty after 24h for no
    real reason.
    """
    products_path = app_config.stores_dir / "acme.products.json"
    products_path.write_text(json.dumps([{"title": "Wool Overcoat", "price": "£189.00", "variants": [], "available": True, "url": "u"}]))
    # No write_fetched_at() call — an "unknown age" file, which the plain
    # is_stale() check treats as stale by default.
    store = _store()
    store = store.model_copy(update={"demo_created_at": None})
    result, status = handle_search_products(_ctx(app_config, store), {"query": "overcoat"})
    assert status == "ok"
    assert result["stock_data_may_be_stale"] is False
