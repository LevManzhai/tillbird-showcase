import json
from datetime import datetime, timedelta, timezone

from app.catalog import StaticProductCatalog, format_price_summary, parse_price, pick_demo_order_line_items, write_fetched_at


def _write_products(path, products):
    path.write_text(json.dumps(products))


def _variant(title, price, available=True):
    return {"title": title, "price": price, "available": available}


def test_static_catalog_matches_title_substring(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {"title": "Merino Crew Jumper", "variants": [_variant("S", "65.00"), _variant("M", "65.00")], "url": "u1"},
            {"title": "Wool Overcoat", "variants": [_variant("10", "189.00")], "url": "u2"},
        ],
    )
    catalog = StaticProductCatalog(path)
    results = catalog.search("jumper")
    assert len(results) == 1
    assert results[0].title == "Merino Crew Jumper"


def test_static_catalog_case_insensitive(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Wool Overcoat", "variants": [_variant("10", "189.00")], "url": ""}])
    catalog = StaticProductCatalog(path)
    assert len(catalog.search("OVERCOAT")) == 1


def test_static_catalog_respects_limit(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [{"title": f"Jumper {i}", "variants": [_variant("One size", "10.00")], "url": ""} for i in range(10)],
    )
    catalog = StaticProductCatalog(path)
    assert len(catalog.search("jumper", limit=5)) == 5


def test_static_catalog_missing_file_returns_empty(tmp_path):
    catalog = StaticProductCatalog(tmp_path / "nope.products.json")
    assert catalog.search("anything") == []


def test_static_catalog_no_match_returns_empty(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Wool Overcoat", "variants": [_variant("10", "189.00")], "url": ""}])
    catalog = StaticProductCatalog(path)
    assert catalog.search("trousers") == []


def test_static_catalog_includes_description(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {
                "title": "Wool Overcoat",
                "variants": [_variant("10", "189.00")],
                "url": "",
                "description": "Falls to mid-calf, 80% wool.",
            }
        ],
    )
    catalog = StaticProductCatalog(path)
    assert catalog.search("overcoat")[0].description == "Falls to mid-calf, 80% wool."


def test_static_catalog_missing_description_defaults_to_empty_string(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Wool Overcoat", "variants": [_variant("10", "189.00")], "url": ""}])
    catalog = StaticProductCatalog(path)
    assert catalog.search("overcoat")[0].description == ""


def test_static_catalog_single_price_when_all_variants_match(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [{"title": "Candle", "variants": [_variant("Small", "26.00"), _variant("Small refill", "26.00")], "url": ""}],
    )
    catalog = StaticProductCatalog(path)
    assert catalog.search("candle")[0].price == "£26.00"


def test_static_catalog_price_range_when_variants_differ(tmp_path):
    """Regression: previously the first variant's price was quoted as if it
    were the only price, so a £18/£26/£34 candle range would be reported as
    just "£18" regardless of size.
    """
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {
                "title": "Candle",
                "variants": [_variant("Small", "18.00"), _variant("Medium", "26.00"), _variant("Large", "34.00")],
                "url": "",
            }
        ],
    )
    catalog = StaticProductCatalog(path)
    result = catalog.search("candle")[0]
    assert result.price == "£18.00–£34.00"
    assert {(v.title, v.price) for v in result.variants} == {("Small", "18.00"), ("Medium", "26.00"), ("Large", "34.00")}


def test_static_catalog_available_if_any_variant_available(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {
                "title": "Candle",
                "variants": [_variant("Small", "18.00", available=False), _variant("Large", "34.00", available=True)],
                "url": "",
            }
        ],
    )
    catalog = StaticProductCatalog(path)
    assert catalog.search("candle")[0].available is True


def test_static_catalog_unavailable_if_all_variants_unavailable(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [{"title": "Candle", "variants": [_variant("Small", "18.00", available=False)], "url": ""}],
    )
    catalog = StaticProductCatalog(path)
    assert catalog.search("candle")[0].available is False


def test_parse_price_handles_currency_symbol_and_commas():
    assert parse_price("£1,234.50") == 1234.50
    assert parse_price("24.99") == 24.99
    assert parse_price(24.99) == 24.99
    assert parse_price(None) is None
    assert parse_price("not a price") is None


def test_format_price_summary_single_vs_range():
    assert format_price_summary([26.00]) == "£26.00"
    assert format_price_summary([26.00, 26.00]) == "£26.00"
    assert format_price_summary([18.00, 26.00, 34.00]) == "£18.00–£34.00"
    assert format_price_summary([]) == ""


# ---------- pick_demo_order_line_items ----------


def test_pick_demo_order_line_items_picks_real_products_and_variant():
    products = [
        {"title": "Dog Chew Toy", "available": True, "variants": [{"title": "Large", "available": True}]},
        {"title": "Dog Bed", "available": True, "variants": [{"title": "Medium", "available": True}]},
    ]
    items = pick_demo_order_line_items(products)
    assert items == [
        {"title": "Dog Chew Toy", "variant": "Large", "quantity": 1},
        {"title": "Dog Bed", "variant": "Medium", "quantity": 1},
    ]


def test_pick_demo_order_line_items_respects_max_items():
    products = [{"title": f"Item {i}", "available": True, "variants": []} for i in range(5)]
    items = pick_demo_order_line_items(products, max_items=2)
    assert len(items) == 2


def test_pick_demo_order_line_items_treats_default_title_as_no_variant():
    products = [{"title": "Candle", "available": True, "variants": [{"title": "Default Title", "available": True}]}]
    items = pick_demo_order_line_items(products)
    assert items == [{"title": "Candle", "variant": None, "quantity": 1}]


def test_pick_demo_order_line_items_prefers_in_stock_products():
    products = [
        {"title": "Sold Out Jumper", "available": False, "variants": []},
        {"title": "In Stock Scarf", "available": True, "variants": []},
    ]
    items = pick_demo_order_line_items(products, max_items=1)
    assert items == [{"title": "In Stock Scarf", "variant": None, "quantity": 1}]


def test_pick_demo_order_line_items_falls_back_to_out_of_stock_if_nothing_available():
    products = [{"title": "Sold Out Jumper", "available": False, "variants": []}]
    items = pick_demo_order_line_items(products)
    assert items == [{"title": "Sold Out Jumper", "variant": None, "quantity": 1}]


def test_pick_demo_order_line_items_prefers_in_stock_variant():
    products = [
        {
            "title": "Jumper",
            "available": True,
            "variants": [
                {"title": "S", "available": False},
                {"title": "M", "available": True},
            ],
        }
    ]
    items = pick_demo_order_line_items(products)
    assert items == [{"title": "Jumper", "variant": "M", "quantity": 1}]


def test_pick_demo_order_line_items_empty_products_returns_empty():
    assert pick_demo_order_line_items([]) == []


def test_static_catalog_matches_description_substring(tmp_path):
    """Regression: an allergen/ingredient search (e.g. "lime") needs to find
    products where the term only appears in the description, not the title —
    a citrus-allergy search that only checks titles misses real matches.
    """
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {
                "title": "Sayulita Candle",
                "variants": [_variant("Small", "18.00")],
                "url": "",
                "description": "Notes: vanilla, coconut water & lime.",
            },
            {"title": "Bothy Candle", "variants": [_variant("Small", "18.00")], "url": "", "description": "Notes: vanilla, cedarwood & cade."},
        ],
    )
    catalog = StaticProductCatalog(path)
    results = catalog.search("lime")
    assert [r.title for r in results] == ["Sayulita Candle"]


def test_static_catalog_matches_any_of_comma_separated_terms(tmp_path):
    """A single search call can check several synonyms at once (e.g. an
    allergen search across citrus/lime/bergamot) instead of needing one tool
    call per synonym.
    """
    path = tmp_path / "acme.products.json"
    _write_products(
        path,
        [
            {"title": "Linen Candle", "variants": [_variant("Small", "18.00")], "url": "", "description": "Notes: jasmine, citrus & musk."},
            {"title": "Sayulita Candle", "variants": [_variant("Small", "18.00")], "url": "", "description": "Notes: vanilla & lime."},
            {"title": "Amber Candle", "variants": [_variant("Small", "18.00")], "url": "", "description": "Notes: sandalwood & bergamot."},
            {"title": "Bothy Candle", "variants": [_variant("Small", "18.00")], "url": "", "description": "Notes: vanilla & cedarwood."},
        ],
    )
    catalog = StaticProductCatalog(path)
    results = catalog.search("citrus, lime, bergamot")
    assert {r.title for r in results} == {"Linen Candle", "Sayulita Candle", "Amber Candle"}


def test_static_catalog_is_stale_when_no_meta_file(tmp_path):
    """Unknown scrape age is treated as stale by default, so the agent hedges
    rather than assuming freshness it can't verify (e.g. a demo created
    before daily refresh existed, with no meta file yet)."""
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Candle", "variants": [_variant("Small", "18.00")], "url": ""}])
    catalog = StaticProductCatalog(path)
    assert catalog.is_stale() is True


def test_static_catalog_not_stale_when_recently_fetched(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Candle", "variants": [_variant("Small", "18.00")], "url": ""}])
    write_fetched_at(path, datetime.now(timezone.utc) - timedelta(hours=1))
    catalog = StaticProductCatalog(path)
    assert catalog.is_stale() is False


def test_static_catalog_stale_when_older_than_threshold(tmp_path):
    path = tmp_path / "acme.products.json"
    _write_products(path, [{"title": "Candle", "variants": [_variant("Small", "18.00")], "url": ""}])
    write_fetched_at(path, datetime.now(timezone.utc) - timedelta(hours=25))
    catalog = StaticProductCatalog(path)
    assert catalog.is_stale() is True
