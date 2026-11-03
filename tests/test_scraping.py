import json

import pytest
import requests
from bs4 import BeautifulSoup

from app.catalog import ProductResult
from app.scraping import (
    FAQ_PATH_CANDIDATES,
    PoliteFetcher,
    ScrapeError,
    detect_brand_color,
    detect_shop_name,
    fetch_all_products,
    fetch_faq_text,
    fetch_homepage,
    fetch_policy_texts,
    fetch_product_care_text,
    fetch_product_pages_text,
    find_faq_link,
    is_password_protected,
    is_shopify_store,
    normalise_base_url,
    strip_html_to_text,
    strip_html_to_text_keep_banner,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def _install_fake_routes(monkeypatch, routes: dict):
    """routes: path (without query string special-cased) -> FakeResponse, or a
    callable(path, query) -> FakeResponse. "/robots.txt" defaults to empty/allow-all
    unless overridden.
    """

    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        path = parsed.path
        query = parsed.query
        if path == "/robots.txt" and "/robots.txt" not in routes:
            return FakeResponse(status_code=404, text="")
        key = path if path in routes else f"{path}?{query}" if f"{path}?{query}" in routes else None
        if key is None:
            # try matching any route registered with just the path prefix
            for route_key, route_val in routes.items():
                if route_key.split("?")[0] == path:
                    key = route_key
                    break
        if key is None:
            return FakeResponse(status_code=404, text="")
        val = routes[key]
        return val(path, query) if callable(val) else val

    monkeypatch.setattr(requests.Session, "get", fake_get)


def test_normalise_base_url_adds_scheme():
    assert normalise_base_url("emberhome.uk") == "https://emberhome.uk"


def test_normalise_base_url_keeps_existing_scheme():
    assert normalise_base_url("http://emberhome.uk/some/path") == "http://emberhome.uk"


def test_normalise_base_url_rejects_garbage():
    with pytest.raises(ScrapeError):
        normalise_base_url("not a url at all")


def test_is_shopify_store_true_when_products_json_present(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/products.json": FakeResponse(200, json_data={"products": []})},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert is_shopify_store(fetcher) is True


def test_is_shopify_store_false_for_non_shopify_site(monkeypatch):
    _install_fake_routes(monkeypatch, {"/products.json": FakeResponse(404, text="not found")})
    fetcher = PoliteFetcher("https://not-shopify.example", min_delay=0)
    assert is_shopify_store(fetcher) is False


def test_is_password_protected_detects_password_form(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/": FakeResponse(200, text='<html><form><input name="password"></form></html>')},
    )
    fetcher = PoliteFetcher("https://locked.example", min_delay=0)
    assert is_password_protected(fetcher) is True


def test_is_password_protected_false_for_normal_site(monkeypatch):
    _install_fake_routes(monkeypatch, {"/": FakeResponse(200, text="<html><body>Welcome</body></html>")})
    fetcher = PoliteFetcher("https://open.example", min_delay=0)
    assert is_password_protected(fetcher) is False


def test_is_password_protected_detects_401(monkeypatch):
    _install_fake_routes(monkeypatch, {"/": FakeResponse(401, text="")})
    fetcher = PoliteFetcher("https://locked2.example", min_delay=0)
    assert is_password_protected(fetcher) is True


def test_robots_txt_disallow_blocks_fetch(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {
            "/robots.txt": FakeResponse(200, text="User-agent: *\nDisallow: /secret\n"),
            "/secret": FakeResponse(200, text="should not be reachable"),
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetcher.get("/secret") is None


def test_robots_txt_allows_unlisted_paths(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {
            "/robots.txt": FakeResponse(200, text="User-agent: *\nDisallow: /secret\n"),
            "/products.json": FakeResponse(200, json_data={"products": []}),
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetcher.get("/products.json") is not None


def test_fetch_all_products_parses_and_paginates(monkeypatch):
    page1 = {
        "products": [
            {
                "title": "Wool Overcoat",
                "handle": "wool-overcoat",
                "body_html": "<p>Falls to <b>mid-calf</b>.</p>",
                "variants": [{"title": "UK 12", "price": "189.00", "available": True}],
            }
        ]
        * 2  # 2 products, less than page_size so pagination stops
    }

    def products_route(path, query):
        return FakeResponse(200, json_data=page1)

    _install_fake_routes(monkeypatch, {"/products.json": products_route})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = fetch_all_products(fetcher, page_size=250)
    assert len(products) == 2
    assert products[0].title == "Wool Overcoat"
    assert products[0].price == "£189.00"
    assert products[0].description == "Falls to mid-calf ."
    assert products[0].available is True
    assert products[0].url == "https://acme.example/products/wool-overcoat"


def test_fetch_all_products_stops_on_empty_page(monkeypatch):
    _install_fake_routes(monkeypatch, {"/products.json": FakeResponse(200, json_data={"products": []})})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetch_all_products(fetcher) == []


def test_fetch_policy_texts_only_includes_found_pages(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {
            "/policies/shipping-policy": FakeResponse(200, text="<p>We ship in 1-3 days.</p>"),
            "/policies/refund-policy": FakeResponse(404, text=""),
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "shipping" in texts
    assert "We ship in 1-3 days." in texts["shipping"]
    assert "returns" not in texts


def test_fetch_policy_texts_finds_about_page_under_first_matching_candidate(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/pages/our-story": FakeResponse(200, text="<p>Our candles are pet-safe and vegan.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "about" in texts
    assert "pet-safe and vegan" in texts["about"]


def test_fetch_policy_texts_stops_at_first_about_candidate_found():
    # /pages/about is tried before /pages/our-story — if both existed, the
    # first one found should win rather than being silently overwritten.
    from app.scraping import ABOUT_PATH_CANDIDATES

    assert ABOUT_PATH_CANDIDATES.index("/pages/about") < ABOUT_PATH_CANDIDATES.index("/pages/our-story")


def test_fetch_policy_texts_no_about_key_when_no_candidate_found(monkeypatch):
    _install_fake_routes(monkeypatch, {})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "about" not in texts


def test_fetch_policy_texts_finds_faq_at_the_plural_pages_faqs(monkeypatch):
    """Regression: ohmymelt.co.uk's FAQ is at /pages/faqs (plural) — this
    used to be hardcoded to /pages/faq (singular) only, so its
    subscription/vegan/paraffin-free answers were never scraped."""
    _install_fake_routes(
        monkeypatch,
        {"/pages/faqs": FakeResponse(200, text="<p>Yes, our melts are vegan and paraffin-free.</p>")},
    )
    fetcher = PoliteFetcher("https://ohmymelt.co.uk", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "faq" in texts
    assert "vegan and paraffin-free" in texts["faq"]


def test_fetch_policy_texts_faq_singular_tried_before_plural():
    assert FAQ_PATH_CANDIDATES.index("/pages/faq") < FAQ_PATH_CANDIDATES.index("/pages/faqs")


def test_fetch_policy_texts_finds_faq_via_footer_link_when_no_path_guess_matches(monkeypatch):
    homepage_html = '<html><body><footer><a href="/pages/help-centre/faqs">FAQs</a></footer></body></html>'
    _install_fake_routes(
        monkeypatch,
        {
            "/": FakeResponse(200, text=homepage_html),
            "/pages/help-centre/faqs": FakeResponse(200, text="<p>Subscriptions can be paused any time.</p>"),
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    homepage_soup = fetch_homepage(fetcher)
    texts = fetch_policy_texts(fetcher, homepage_soup=homepage_soup)
    assert "faq" in texts
    assert "Subscriptions can be paused" in texts["faq"]


def test_fetch_policy_texts_finds_faq_via_app_embedded_a_faq(monkeypatch):
    _install_fake_routes(monkeypatch, {"/a/faq": FakeResponse(200, text="<p>Yes, we ship internationally.</p>")})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "faq" in texts
    assert "ship internationally" in texts["faq"]


def test_fetch_policy_texts_no_faq_key_when_nothing_found(monkeypatch):
    _install_fake_routes(monkeypatch, {})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "faq" not in texts


def test_find_faq_link_ignores_an_external_domain_link():
    homepage_html = '<html><body><a href="https://zendesk.example/faqs">FAQs</a></body></html>'
    soup = BeautifulSoup(homepage_html, "html.parser")
    assert find_faq_link(soup, "https://acme.example") is None


def test_find_faq_link_matches_whole_word_only():
    """"faqtory" (a made-up brand/product name) must not match \bfaqs?\b."""
    homepage_html = '<html><body><a href="/pages/faqtory-candles">Faqtory candles</a></body></html>'
    soup = BeautifulSoup(homepage_html, "html.parser")
    assert find_faq_link(soup, "https://acme.example") is None


def test_fetch_faq_text_returns_none_when_nothing_found(monkeypatch):
    _install_fake_routes(monkeypatch, {})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetch_faq_text(fetcher) is None


def test_fetch_policy_texts_finds_dedicated_care_page(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/pages/garment-care": FakeResponse(200, text="<p>Hand wash only, lay flat to dry.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "care" in texts
    assert "Hand wash only" in texts["care"]


def test_fetch_policy_texts_rejects_care_candidate_page_with_no_care_content(monkeypatch):
    """Regression: A knitwear store's /pages/size-guide is a real page (a fit
    quiz) that returns 200 — the care-candidate loop accepted it as "care"
    just because it was found at a guessed URL, even though it contains no
    actual washing/care instructions. That silently skipped the more
    reliable fetch_product_care_text fallback in build_demo, so a real
    washing question got answered with "no washing guide to hand" despite
    the real instructions being right there on every product page.
    """
    _install_fake_routes(
        monkeypatch,
        {"/pages/size-guide": FakeResponse(200, text="<p>Tell us your usual size and we'll find your best fit.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "care" not in texts


def test_fetch_policy_texts_finds_genuine_size_guide_page(monkeypatch):
    """Regression: A clothing store's "do your t-shirts run true to
    size?" got "no size info" even though /pages/size-guide has a full
    UK/US/EU measurement chart — size-guide content was only ever captured
    under the "care" key, gated on care/washing keywords, so a page with
    real sizing but no washing instructions was silently dropped entirely.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/pages/size-guide": FakeResponse(
                200,
                text="<p>Our clothing is fitted on real women. Bust: 86cm, Waist: 71cm, Hips: 98cm. "
                "How to measure: use a soft tape measure.</p>",
            )
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "size_guide" in texts
    assert "86cm" in texts["size_guide"]


def test_fetch_policy_texts_rejects_size_guide_candidate_with_no_real_sizing_content(monkeypatch):
    """Regression (the actual live bug): A knitwear store's /pages/size-guide
    exists and returns 200, but its real sizing tool is client-side JS — the
    server-rendered page is just nav chrome with the words "Size Guide" in
    the title and nothing else. Must not be captured as if it had content.
    """
    _install_fake_routes(
        monkeypatch,
        {"/pages/size-guide": FakeResponse(200, text="<title>Size Guide - Mens</title><p>Skip to content</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "size_guide" not in texts


def test_fetch_policy_texts_finds_dedicated_delivery_info_page(monkeypatch):
    """Regression: Teddy Eva Scents' homepage FAQ stated a stale "2-3
    working days", while its own dedicated /pages/delivery-information page
    said the current "1-2 working days" — but that page was never scraped
    at all, so the drafting prompt only ever saw the stale homepage figure.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/pages/delivery-information": FakeResponse(
                200,
                text="<p>Dispatch time: we aim to dispatch orders within 1-2 working days via Royal Mail "
                "Tracked 48. £3.25, free over £30.</p>",
            )
        },
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "delivery_info" in texts
    assert "1-2 working days" in texts["delivery_info"]


def test_fetch_policy_texts_rejects_delivery_candidate_with_no_delivery_content(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/pages/delivery-information": FakeResponse(200, text="<p>Nothing relevant here at all.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "delivery_info" not in texts


def test_fetch_policy_texts_finds_dedicated_refund_info_page(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/pages/returns-information": FakeResponse(200, text="<p>You can return items within 14 days for a refund.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "refund_info" in texts
    assert "14 days" in texts["refund_info"]


def test_fetch_policy_texts_rejects_refund_candidate_with_no_refund_content(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/pages/returns-information": FakeResponse(200, text="<p>Nothing relevant here at all.</p>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "refund_info" not in texts


def test_fetch_policy_texts_finds_promo_badge_on_collections_all(monkeypatch):
    """Regression (the actual live bug): charlartscrafts.co.uk renders "5
    for £15" on nearly every wax melt product card on /collections/all,
    but nothing scraped ever mentioned it — the demo bot flatly denied any
    bulk discount existed when a real customer-style question asked about
    buying 5 wax melts.
    """
    html = (
        "<html><body>"
        '<div class="product-card"><label class="lbl pr-label2">5 for £15</label>'
        "<h3>Snow Fairy Wax Melt</h3></div>"
        '<div class="product-card"><label class="lbl pr-label2">5 for £15</label>'
        "<h3>Dior Sauvage Wax Melt</h3></div>"
        "</body></html>"
    )
    _install_fake_routes(monkeypatch, {"/collections/all": FakeResponse(200, text=html)})
    fetcher = PoliteFetcher("https://charlartscrafts.co.uk", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "promotions" in texts
    assert "5 for £15" in texts["promotions"]


def test_fetch_policy_texts_dedupes_repeated_promo_badges(monkeypatch):
    """The same badge repeats on every product card — only the first
    occurrence of each distinct phrase should be kept, not one snippet per
    product (which would otherwise bloat the drafting prompt with dozens
    of near-identical copies of the same deal).
    """
    html = "<html><body>" + "<label>5 for £15</label><h3>Melt</h3>" * 20 + "</body></html>"
    _install_fake_routes(monkeypatch, {"/collections/all": FakeResponse(200, text=html)})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert texts["promotions"] == "5 for £15"


def test_fetch_policy_texts_no_promotions_key_when_nothing_found(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/collections/all": FakeResponse(200, text="<html><body>Just some lovely candles.</body></html>")},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "promotions" not in texts


def test_fetch_policy_texts_promotions_absent_when_collections_all_missing(monkeypatch):
    _install_fake_routes(monkeypatch, {})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert "promotions" not in texts


def test_fetch_policy_texts_caps_each_page_at_max_length(monkeypatch):
    from app.scraping import MAX_PAGE_TEXT_CHARS

    huge = "word " * 10000  # comfortably over MAX_PAGE_TEXT_CHARS
    _install_fake_routes(monkeypatch, {"/policies/shipping-policy": FakeResponse(200, text=f"<p>{huge}</p>")})
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    texts = fetch_policy_texts(fetcher)
    assert len(texts["shipping"]) <= MAX_PAGE_TEXT_CHARS


# ---------- fetch_product_care_text ----------


def test_fetch_product_care_text_finds_care_snippet_on_product_page(monkeypatch):
    def fake_get(self, url, timeout=None):
        if "/products/jumper" in url:
            return FakeResponse(
                200,
                text="<html><body>Lovely jumper. Garment Care: hand wash only in cool water, never tumble dry.</body></html>",
            )
        return FakeResponse(404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [ProductResult(title="Jumper", price="£50", url="https://acme.example/products/jumper")]

    text = fetch_product_care_text(fetcher, products)
    assert "hand wash only" in text.lower()


def test_fetch_product_care_text_stops_at_first_match(monkeypatch):
    calls = []

    def fake_get(self, url, timeout=None):
        calls.append(url)
        if "/products/a" in url:
            return FakeResponse(200, text="<html><body>Garment care: dry clean only.</body></html>")
        return FakeResponse(200, text="<html><body>No care info here.</body></html>")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [
        ProductResult(title="A", price="£1", url="https://acme.example/products/a"),
        ProductResult(title="B", price="£1", url="https://acme.example/products/b"),
    ]

    text = fetch_product_care_text(fetcher, products)
    assert "dry clean only" in text.lower()
    assert not any("/products/b" in c for c in calls)


def test_fetch_product_care_text_returns_empty_when_no_product_mentions_care(monkeypatch):
    def fake_get(self, url, timeout=None):
        return FakeResponse(200, text="<html><body>Just a lovely jumper, no care details.</body></html>")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [ProductResult(title="A", price="£1", url="https://acme.example/products/a")]

    assert fetch_product_care_text(fetcher, products) == ""


def test_fetch_product_care_text_handles_no_products():
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetch_product_care_text(fetcher, []) == ""


def test_detect_shop_name_from_og_site_name(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/": FakeResponse(200, text='<html><head><meta property="og:site_name" content="Acme Fashion"></head></html>')},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    soup = fetch_homepage(fetcher)
    assert detect_shop_name(soup, "https://acme.example") == "Acme Fashion"


def test_detect_shop_name_falls_back_to_domain():
    assert detect_shop_name(None, "https://emberhome.uk") == "Emberhome"


def test_detect_shop_name_prefers_logo_alt_over_a_generic_title():
    """Regression: ohmymelt.co.uk's <title> led with the generic product
    category ("Wax Melts | Oh My Melt"), so title-splitting alone picked
    "Wax Melts" — no og:site_name was present, but the header logo's own
    alt text correctly said "Oh My Melt".
    """
    html = (
        "<html><head><title>Wax Melts | Oh My Melt</title></head>"
        '<body><header><a class="header__logo-link">'
        '<img class="header__logo" alt="Oh My Melt" src="/logo.svg"></a></header></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    assert detect_shop_name(soup, "https://ohmymelt.co.uk") == "Oh My Melt"


def test_detect_shop_name_og_site_name_still_wins_over_logo_alt():
    html = (
        '<html><head><meta property="og:site_name" content="Acme Fashion"></head>'
        '<body><img class="site-logo" alt="Wrong Name"></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    assert detect_shop_name(soup, "https://acme.example") == "Acme Fashion"


def test_detect_shop_name_ignores_a_non_logo_image_alt():
    html = (
        "<html><head><title>Wax Melts | Oh My Melt</title></head>"
        '<body><img alt="A photo of a candle" src="/product.jpg"></body></html>'
    )
    soup = BeautifulSoup(html, "html.parser")
    # No logo-ish image, so falls through to the (imperfect) title split.
    assert detect_shop_name(soup, "https://ohmymelt.co.uk") == "Wax Melts"


def test_detect_brand_color_from_theme_color_meta(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/": FakeResponse(200, text='<html><head><meta name="theme-color" content="#8a4b2f"></head></html>')},
    )
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    soup = fetch_homepage(fetcher)
    assert detect_brand_color(soup) == "#8a4b2f"


def test_detect_brand_color_none_when_absent():
    assert detect_brand_color(None) is None


def test_strip_html_to_text_removes_scripts_and_collapses_whitespace():
    html = "<html><script>evil()</script><body>  Hello   <b>world</b>  </body></html>"
    assert strip_html_to_text(html) == "Hello world"


def test_strip_html_to_text_removes_country_currency_selector_boilerplate():
    """Regression: a real store's homepage had a ~190-country currency
    selector (not inside a <header>/<footer> landmark on that theme, so the
    tag-based strip above didn't catch it) sitting right before a brand
    claim ("Vegan, Family + Pet Safe") — draft_knowledge_base truncates its
    input, so that claim was silently pushed past the cutoff and the demo
    bot answered a pet-safety question with "I don't have that
    information" even though the store states it plainly. The selector
    must be stripped so real content isn't crowded out.
    """
    import string

    letters = string.ascii_uppercase
    countries = " ".join(f"Country{letters[i % 26]} (GBP £)" for i in range(30))
    html = f"<html><body>{countries} Vegan, Family + Pet Safe. Eco-Friendly + Plastic Free.</body></html>"
    text = strip_html_to_text(html)
    assert "CountryA" not in text
    assert "CountryZ" not in text
    assert "Vegan, Family + Pet Safe" in text


def test_strip_html_to_text_does_not_strip_normal_prose_with_parens():
    html = "<html><body>Price £24.99 (was £29.99). Available in Small (S), Medium (M), Large (L).</body></html>"
    text = strip_html_to_text(html)
    assert "£24.99" in text
    assert "Small (S)" in text


def test_strip_html_to_text_keep_banner_preserves_header_announcement():
    """Regression risk for the audit feature: a header-nested announcement
    bar ("30 DAY RETURNS") is exactly the kind of claim app.audit needs to
    cross-check against the real returns policy — the normal strip_html_to_text
    drops <header> entirely, so this variant must not.
    """
    html = "<html><body><header>30 DAY RETURNS. FREE DELIVERY OVER £30.</header><main>Welcome.</main></body></html>"
    text = strip_html_to_text_keep_banner(html)
    assert "30 DAY RETURNS" in text
    assert "FREE DELIVERY OVER £30" in text
    assert "Welcome" in text


def test_strip_html_to_text_keep_banner_still_strips_scripts():
    html = "<html><body><script>evil()</script><header>Free shipping</header></body></html>"
    text = strip_html_to_text_keep_banner(html)
    assert "evil" not in text
    assert "Free shipping" in text


def test_fetch_product_pages_text_returns_url_tagged_pages(monkeypatch):
    def fake_get(self, url, timeout=None):
        if "/products/jumper" in url:
            return FakeResponse(200, text="<html><body>Garment Care: hand wash only.</body></html>")
        return FakeResponse(404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [ProductResult(title="Jumper", price="£50", url="https://acme.example/products/jumper")]

    pages = fetch_product_pages_text(fetcher, products)
    assert len(pages) == 1
    url, text = pages[0]
    assert url == "https://acme.example/products/jumper"
    assert "hand wash only" in text.lower()


def test_fetch_product_pages_text_skips_products_with_no_page(monkeypatch):
    def fake_get(self, url, timeout=None):
        return FakeResponse(404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [ProductResult(title="A", price="£1", url="https://acme.example/products/a")]

    assert fetch_product_pages_text(fetcher, products) == []


def test_fetch_product_pages_text_respects_max_products(monkeypatch):
    calls = []

    def fake_get(self, url, timeout=None):
        calls.append(url)
        return FakeResponse(200, text="<html><body>Some content here.</body></html>")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    products = [ProductResult(title=f"P{i}", price="£1", url=f"https://acme.example/products/p{i}") for i in range(10)]

    pages = fetch_product_pages_text(fetcher, products, max_products=2)
    assert len(pages) == 2


def test_fetcher_get_returns_none_on_request_exception(monkeypatch):
    def raising_get(self, url, timeout=None):
        raise requests.exceptions.Timeout("simulated timeout")

    monkeypatch.setattr(requests.Session, "get", raising_get)
    fetcher = PoliteFetcher("https://acme.example", min_delay=0)
    assert fetcher.get("/anything") is None
