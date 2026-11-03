import json

import pytest
import requests

from app.config import AppConfig
from app.scraping import ScrapeError
from scripts.create_demo import DemoBuildError, build_demo, main, public_demo_url


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


@pytest.fixture(autouse=True)
def _no_rate_limit_sleep(monkeypatch):
    # build_demo() constructs its own PoliteFetcher() with the real default
    # min_delay (1s) — fine in production, but it'd make this file's tests
    # take 30+ real seconds for no reason. Only time.sleep is stubbed; the
    # rate-limiting logic itself still runs, just without actually waiting.
    monkeypatch.setattr("app.scraping.time.sleep", lambda seconds: None)


def _install_fake_routes(monkeypatch, routes: dict):
    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/robots.txt" and "/robots.txt" not in routes:
            return FakeResponse(status_code=404, text="")
        if path in routes:
            val = routes[path]
            return val(path) if callable(val) else val
        return FakeResponse(status_code=404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)


SHOPIFY_PRODUCTS_JSON = {
    "products": [
        {
            "title": "Wool Scarf",
            "handle": "wool-scarf",
            "body_html": "<p>Warm.</p>",
            "variants": [{"title": "One size", "price": "25.00", "available": True}],
        }
    ]
}


def _fake_draft(*args, **kwargs):
    return {
        "tone": "Warm and friendly.",
        "shipping": "Royal Mail, 2-3 days.",
        "returns": "30 days.",
        "sizing": "",
        "faq": [],
        "missing": [],
    }


@pytest.fixture
def app_config():
    config = AppConfig()
    config.anthropic_api_key = "sk-ant-test"
    return config


def test_build_demo_writes_yaml_and_products(tmp_path, monkeypatch, app_config):
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/policies/shipping-policy": FakeResponse(200, text="<p>Ships fast.</p>"),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)

    result = build_demo("https://acme.example", tmp_path, app_config, store_id="demo-test123")

    assert result["store_id"] == "demo-test123"
    assert len(result["products"]) == 1
    assert result["products"][0].title == "Wool Scarf"
    assert "shipping" in result["policy_texts"]
    assert result["yaml_path"].is_file()
    assert result["products_path"].is_file()

    with result["products_path"].open() as f:
        on_disk = json.load(f)
    assert on_disk[0]["title"] == "Wool Scarf"


def test_main_store_id_flag_overwrites_existing_demo_in_place(tmp_path, monkeypatch, app_config):
    """Regression need: regenerating a demo after a generator fix (e.g. the
    Teddy Eva Scents delivery-info bug) must keep the same store_id/URL,
    not mint a fresh slug — the CLI needs an explicit override for this
    single-store workflow (batch_demos.py already has its own for the CSV
    pipeline).
    """
    import sys

    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)
    monkeypatch.setattr("scripts.create_demo.AppConfig", lambda: app_config)

    old_argv = sys.argv
    try:
        sys.argv = [
            "create_demo.py", "https://acme.example",
            "--stores-dir", str(tmp_path),
            "--store-id", "demo-existing-teddy",
        ]
        rc = main()
    finally:
        sys.argv = old_argv

    assert rc == 0
    assert (tmp_path / "demo-existing-teddy.yaml").is_file()


def test_build_demo_overwrite_in_place_preserves_existing_overrides(tmp_path, monkeypatch, app_config):
    """B4 spec: a manual correction must never be wiped by a re-scrape —
    including regenerating a demo in place with --store-id, which
    otherwise rewrites the whole yaml from scratch."""
    import yaml

    store_id = "demo-existing-teddy"
    (tmp_path / f"{store_id}.yaml").write_text(
        yaml.safe_dump({"store_id": store_id, "overrides": [{"topic": "dispatch time", "answer": "1-2 working days."}]})
    )

    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)

    build_demo("https://acme.example", tmp_path, app_config, store_id=store_id)

    with (tmp_path / f"{store_id}.yaml").open() as f:
        written = yaml.safe_load(f)
    assert written["overrides"] == [{"topic": "dispatch time", "answer": "1-2 working days."}]


def test_build_demo_sets_demo_order_line_items_from_real_catalog(tmp_path, monkeypatch, app_config):
    """Regression: the demo order (order 1001) showed a hardcoded "Merino
    Crew Jumper (size M)" for every store regardless of what it actually
    sells — nonsensical for e.g. a pet shop. Must be built from the
    store's own real products/variants.
    """
    import yaml

    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)

    result = build_demo("https://acme.example", tmp_path, app_config, store_id="demo-test123")

    with result["yaml_path"].open() as f:
        on_disk_config = yaml.safe_load(f)
    assert on_disk_config["demo_order_line_items"] == [
        {"title": "Wool Scarf", "variant": "One size", "quantity": 1}
    ]


def test_build_demo_excludes_privacy_and_terms_text_from_drafting_prompt(tmp_path, monkeypatch, app_config):
    """Regression: privacy-policy/terms-of-service text is pure legal
    boilerplate a support bot never needs to quote, but at up to 5000
    chars each (MAX_PAGE_TEXT_CHARS) it was eating enough of the drafting
    prompt's truncation budget to push "care"/"about" (added last) past
    the cutoff entirely on a store with long legal pages — confirmed live:
    a real washing question got no answer even though the real care text
    had been correctly scraped, purely because of this ordering/budget
    issue. privacy/terms must still be fetched (for privacy_policy_url)
    but never reach the drafting prompt.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/policies/privacy-policy": FakeResponse(200, text="<p>Secret privacy legal text.</p>"),
            "/policies/terms-of-service": FakeResponse(200, text="<p>Secret terms legal text.</p>"),
        },
    )
    captured = {}

    def _capturing_draft(app_config, store_name, scraped_pages, homepage_text="", **kwargs):
        captured["scraped_pages"] = scraped_pages
        return _fake_draft()

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _capturing_draft)

    result = build_demo("https://acme.example", tmp_path, app_config)

    assert "privacy" not in captured["scraped_pages"]
    assert "terms" not in captured["scraped_pages"]
    # privacy_policy_url still correctly set from the unfiltered policy_texts
    assert result["policy_texts"]["privacy"] == "Secret privacy legal text."


def test_build_demo_falls_back_to_product_page_care_text_when_no_care_page(tmp_path, monkeypatch, app_config):
    """Regression: A knitwear store's demo answered "how should I wash my
    jumper?" with "no washing guide to hand" — the washing instructions
    were only on the individual product page's "Garment Care" accordion,
    not in /products.json's body_html and not on any dedicated care page.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/products/wool-scarf": FakeResponse(
                200, text="<html><body>Nice scarf. Garment Care: hand wash only, lay flat to dry.</body></html>"
            ),
        },
    )
    captured = {}

    def _capturing_draft(app_config, store_name, scraped_pages, homepage_text="", **kwargs):
        captured["scraped_pages"] = scraped_pages
        return _fake_draft()

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _capturing_draft)

    build_demo("https://acme.example", tmp_path, app_config)

    assert "care" in captured["scraped_pages"]
    assert "hand wash only" in captured["scraped_pages"]["care"].lower()


def test_build_demo_falls_back_to_product_page_when_care_candidate_has_no_care_content(tmp_path, monkeypatch, app_config):
    """Regression (the actual live bug): a store's /pages/size-guide exists
    and returns 200, but is a pure fit-sizing quiz with no washing
    instructions — this must not block the product-page fallback from
    finding the real care text.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/pages/size-guide": FakeResponse(200, text="<p>Tell us your usual size to find your best fit.</p>"),
            "/products/wool-scarf": FakeResponse(200, text="<html><body>Garment Care: hand wash only.</body></html>"),
        },
    )
    captured = {}

    def _capturing_draft(app_config, store_name, scraped_pages, homepage_text="", **kwargs):
        captured["scraped_pages"] = scraped_pages
        return _fake_draft()

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _capturing_draft)

    build_demo("https://acme.example", tmp_path, app_config)

    assert "hand wash only" in captured["scraped_pages"]["care"].lower()


def test_build_demo_prefers_dedicated_care_page_over_product_page(tmp_path, monkeypatch, app_config):
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/pages/care": FakeResponse(200, text="<p>Dry clean only.</p>"),
            "/products/wool-scarf": FakeResponse(200, text="<html><body>Garment Care: hand wash only.</body></html>"),
        },
    )
    captured = {}

    def _capturing_draft(app_config, store_name, scraped_pages, homepage_text="", **kwargs):
        captured["scraped_pages"] = scraped_pages
        return _fake_draft()

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _capturing_draft)

    build_demo("https://acme.example", tmp_path, app_config)

    assert captured["scraped_pages"]["care"] == "Dry clean only."


def test_build_demo_writes_promotions_scraped_from_collections_page(tmp_path, monkeypatch, app_config):
    """Regression: the written store YAML's knowledge_base had no
    "promotions" key at all — build_demo() must carry through the
    deterministic template app.demo_kb.draft_promotions builds from
    whatever app.scraping found on /collections/all, since that field is
    never drafted by the LLM (see draft_promotions's own docstring).
    """
    import yaml

    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/collections/all": FakeResponse(200, text="<html><body><label>5 for £15</label></body></html>"),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)

    result = build_demo("https://acme.example", tmp_path, app_config, store_id="demo-test123")

    with result["yaml_path"].open() as f:
        on_disk_config = yaml.safe_load(f)
    assert "5 for £15" in on_disk_config["knowledge_base"]["promotions"]


def test_build_demo_generates_slug_when_store_id_not_given(tmp_path, monkeypatch, app_config):
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
        },
    )
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _fake_draft)

    result = build_demo("https://acme.example", tmp_path, app_config)
    assert result["store_id"].startswith("demo-")


def test_build_demo_raises_scrape_error_for_bad_url(tmp_path, app_config):
    with pytest.raises(ScrapeError):
        build_demo("not a url at all", tmp_path, app_config)


def test_build_demo_raises_for_non_shopify_store(tmp_path, monkeypatch, app_config):
    _install_fake_routes(monkeypatch, {"/products.json": FakeResponse(404, text="not found")})
    with pytest.raises(DemoBuildError, match="doesn't look like a Shopify store"):
        build_demo("https://not-shopify.example", tmp_path, app_config)


def test_build_demo_raises_for_password_protected_store(tmp_path, monkeypatch, app_config):
    _install_fake_routes(
        monkeypatch,
        {"/": FakeResponse(200, text='<html><form><input name="password"></form></html>')},
    )
    with pytest.raises(DemoBuildError, match="password-protected"):
        build_demo("https://locked.example", tmp_path, app_config)


def test_build_demo_raises_without_anthropic_key(tmp_path, monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": FakeResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
        },
    )
    config = AppConfig()
    config.anthropic_api_key = ""
    with pytest.raises(DemoBuildError, match="ANTHROPIC_API_KEY"):
        build_demo("https://acme.example", tmp_path, config)


def test_public_demo_url_uses_override_first(app_config):
    app_config.public_base_url = "https://app.tillbird.co.uk"
    url = public_demo_url(app_config, "demo-abc", public_app_url="https://override.example")
    assert url == "https://override.example/demo/demo-abc"


def test_public_demo_url_falls_back_to_app_config(app_config):
    app_config.public_base_url = "https://app.tillbird.co.uk"
    url = public_demo_url(app_config, "demo-abc")
    assert url == "https://app.tillbird.co.uk/demo/demo-abc"


def test_public_demo_url_falls_back_to_localhost(app_config, monkeypatch):
    app_config.public_base_url = ""
    monkeypatch.delenv("PORT", raising=False)
    url = public_demo_url(app_config, "demo-abc")
    assert url == "http://localhost:8000/demo/demo-abc"
