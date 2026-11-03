import pytest
import requests

from app.catalog import ProductResult
from app.config import AppConfig
from app.demo_kb import StoreProvisioningError, draft_knowledge_base, draft_personal_line, scrape_and_draft_store
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse


class _FakeToolUseBlock:
    def __init__(self, id, name, input):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


def test_draft_knowledge_base_returns_tool_input():
    drafted = {
        "tone": "Warm and concise.",
        "shipping": "Free UK shipping, 7-15 business days.",
        "returns": "30 days, unworn.",
        "sizing": "",
        "faq": [{"question": "How can I contact you?", "answer": "Email us."}],
        "missing": ["Carrier not stated on the site."],
    }
    client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "draft_knowledge_base", drafted)])]
    )

    app_config = AppConfig()
    result = draft_knowledge_base(
        app_config, "Acme", {"shipping": "We ship free in the UK, 7-15 days."}, client=client
    )

    assert result == drafted
    call_kwargs = client.messages.calls[0]
    assert call_kwargs["tool_choice"] == {"type": "auto"}
    assert "draft_knowledge_base" in call_kwargs["messages"][-1]["content"]


def test_draft_knowledge_base_raises_on_truncated_response():
    """Regression (the actual live bug): a verbose store's response hit the
    token cap mid-tool-call, so Claude's partial input simply never reached
    faq/promotions/missing at all — build_demo then wrote an empty
    `promotions` field as if no deal existed, when in fact the real "5 for
    £15" text had been scraped correctly and just never reached the output.
    A truncated response must error, never be accepted as a complete draft.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "max_tokens",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "Warm.", "shipping": "Free UK shipping."})],
            )
        ]
    )
    app_config = AppConfig()
    with pytest.raises(RuntimeError, match="truncated"):
        draft_knowledge_base(app_config, "Acme", {"shipping": "We ship free in the UK."}, client=client)


def test_draft_knowledge_base_raises_if_no_tool_call_returned():
    from tests.fake_anthropic import FakeTextBlock

    # Two text-only replies: the first call plus the one retry create_with_tool makes.
    client = FakeAnthropicClient(
        [FakeResponse("end_turn", [FakeTextBlock("I refuse to use tools.")]) for _ in range(2)]
    )

    app_config = AppConfig()
    with pytest.raises(RuntimeError):
        draft_knowledge_base(app_config, "Acme", {"shipping": "text"}, client=client)


def test_prompt_allows_homepage_as_a_factual_source_with_policy_taking_precedence():
    """Homepage text is the store's own public claims, so it may be used as
    fact (not just for tone) — but a scraped policy page must win if the two
    disagree, and promotional claims must be framed as "check at checkout"
    rather than stated as an ongoing fact.
    """
    drafted = {
        "tone": "Warm and concise.",
        "shipping": "",
        "returns": "",
        "sizing": "",
        "faq": [],
        "missing": [],
    }
    client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "draft_knowledge_base", drafted)])]
    )

    app_config = AppConfig()
    draft_knowledge_base(
        app_config,
        "Acme",
        {"shipping": "We ship free in the UK, 7-15 days."},
        homepage_text="Handmade in Chipping Norton. 20% off everything this weekend!",
        client=client,
    )

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "legitimate source of facts" in prompt
    assert "policy page is authoritative" in prompt
    assert "check the current offer at checkout" in prompt
    assert "Handmade in Chipping Norton" in prompt


def test_prompt_instructs_brand_claims_extraction_from_about_and_faq_pages():
    drafted = {
        "tone": "Warm.",
        "shipping": "",
        "returns": "",
        "sizing": "",
        "brand_claims": "All candles are pet-safe and vegan.",
        "faq": [],
        "missing": [],
    }
    client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "draft_knowledge_base", drafted)])]
    )
    app_config = AppConfig()

    result = draft_knowledge_base(
        app_config,
        "Acme Candles",
        {"about": "We only use pet-safe, vegan fragrance oils in every candle."},
        client=client,
    )

    assert result["brand_claims"] == "All candles are pet-safe and vegan."
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "brand_claims" in prompt
    assert "pet or child" in prompt.lower() or "pet safety" in prompt.lower()
    assert "pet-safe, vegan fragrance oils" in prompt


def test_prompt_forbids_adding_unstated_return_conditions():
    """Regression: a returns answer said "unworn, with tags" for a store
    whose policy text only said "unworn" — the draft step must not pad in
    a plausible-sounding extra condition that isn't actually stated.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []})],
            )
        ]
    )
    app_config = AppConfig()
    draft_knowledge_base(app_config, "Acme", {"returns": "Items must be unworn."}, client=client)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "tags" in prompt.lower()
    assert "never add a condition" in prompt.lower()


def test_prompt_states_delivery_info_and_refund_info_are_most_authoritative():
    """Regression: Teddy Eva Scents' demo said orders dispatch in "2-3
    working days" (a stale homepage FAQ figure) instead of the store's own
    dedicated Delivery Information page's "1-2 working days" — the prompt
    must explicitly rank a dedicated delivery_info/refund_info page above
    both the generic Shopify policy page and the homepage, and forbid
    mixing figures (e.g. days from one source, price from another) across
    sources for the same fact.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []})],
            )
        ]
    )
    app_config = AppConfig()
    draft_knowledge_base(
        app_config,
        "Acme",
        {
            "shipping": "",
            "delivery_info": "We aim to dispatch orders within 1-2 working days via Royal Mail Tracked 48, £3.25.",
        },
        client=client,
    )
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "[delivery_info]" in prompt
    assert "[refund_info]" in prompt
    assert "more authoritative than" in prompt.lower()
    assert "never mix" in prompt.lower() or "never_mix" in prompt.lower().replace(" ", "_")
    assert "1-2 working days via Royal Mail Tracked 48" in prompt


def test_prompt_ranks_policy_pages_above_product_pages_above_homepage():
    """B1 spec, full priority order below delivery_info/refund_info:
    Shopify's generic policy page, then a product page, then the
    homepage/marketing copy — explicit so drafting never prefers a
    product description or homepage blurb over the store's own official
    policy page.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []})],
            )
        ]
    )
    app_config = AppConfig()
    draft_knowledge_base(app_config, "Acme", {"shipping": "Free delivery."}, client=client)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "product page" in prompt.lower()
    assert "the homepage" in prompt.lower()
    # Order matters: policy page ranked above product page, which is
    # ranked above homepage.
    assert prompt.lower().index('"[shipping]"') < prompt.lower().index("a product page")
    assert prompt.lower().index("a product page") < prompt.lower().index("4. the homepage")


def test_prompt_surfaces_a_homepage_only_delivery_fact_as_a_secondary_addition():
    """B1 spec: the authority order picks a winner when sources disagree —
    it must not also mean dropping a genuinely separate delivery/offer
    fact that ONLY the homepage states and no higher-priority source
    mentions at all (confirmed live: riversidewoodcraft.co.uk's "NEXT DAY
    DELIVERY — NOW AVAILABLE..." banner was silently dropped even though
    the delivery page didn't contradict it, it just didn't mention it).
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []})],
            )
        ]
    )
    app_config = AppConfig()
    draft_knowledge_base(app_config, "Acme", {"shipping": "Free delivery."}, client=client)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "does not mean ignoring" in prompt.lower()
    assert "also stated on the site" in prompt.lower()
    assert "next day delivery" in prompt.lower()
    assert "absent" in prompt.lower()
    # Must still only apply when nothing higher-priority conflicts with it.
    assert "if any of them states something that" in prompt.lower()
    assert "actually conflicts" in prompt.lower()


def test_prompt_tells_drafting_llm_to_ignore_the_promotions_section():
    """The drafting prompt must not ask Claude to restate [promotions]
    figures at all — draft_promotions builds that field from code instead
    (see its own docstring for why: no prompt wording reliably stopped
    Claude from padding in an invented second tier).
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [_FakeToolUseBlock("t1", "draft_knowledge_base", {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []})],
            )
        ]
    )
    app_config = AppConfig()
    draft_knowledge_base(app_config, "Acme", {"promotions": "5 for £15"}, client=client)
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "[promotions]" in prompt
    assert "ignore it when drafting" in prompt.lower()


def test_draft_promotions_builds_template_from_scraped_phrases():
    from app.demo_kb import draft_promotions

    result = draft_promotions({"promotions": "5 for £15"})
    assert "5 for £15" in result
    assert "checkout" in result.lower()


def test_draft_promotions_empty_when_nothing_scraped():
    from app.demo_kb import draft_promotions

    assert draft_promotions({}) == ""
    assert draft_promotions({"shipping": "Free UK shipping."}) == ""


def test_draft_promotions_never_adds_figures_beyond_what_was_scraped():
    """Regression (the actual live bug): CharlartsCrafts' real site only
    ever states "5 for £15", but an LLM-drafted promotions field kept
    inventing an extra "10 for £25" tier no matter how the prompt was
    worded. Since draft_promotions is a plain string template, not an LLM
    call, it can only ever contain exactly what app.scraping extracted.
    """
    from app.demo_kb import draft_promotions

    result = draft_promotions({"promotions": "5 for £15"})
    assert "10 for £25" not in result
    assert "25" not in result


def test_scrape_and_draft_store_keeps_announcement_bar_text_in_homepage_text(monkeypatch):
    """Regression: an announcement bar ("5 FOR £15 MIX & MATCH") is usually
    nested inside <header>/<nav> in a theme's markup — strip_html_to_text
    strips those tags as chrome, which meant a promo stated ONLY in the
    banner (not also on /collections/all) never reached the drafting
    prompt's homepage_text at all. Must use strip_html_to_text_keep_banner
    instead, same as app.audit's identical fix for the same reason.
    """
    import requests

    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/":
            return _FakeHttpResponse(
                200, text="<html><header>5 FOR £15 MIX & MATCH</header><body><title>Acme</title></body></html>"
            )
        if path == "/products.json":
            return _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON)
        return _FakeHttpResponse(404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    captured = {}
    drafted = {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "promotions": "", "faq": [], "missing": []}

    def _capturing_draft(app_config, store_name, scraped_pages, homepage_text="", **kwargs):
        captured["homepage_text"] = homepage_text
        return drafted

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _capturing_draft)
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"

    scrape_and_draft_store("https://acme.example", app_config)

    assert "5 FOR £15 MIX & MATCH" in captured["homepage_text"]


def test_draft_personal_line_returns_tool_input():
    client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "draft_personal_line", {"line": "They sell hand-poured soy candles and reed diffusers."})])]
    )
    products = [ProductResult(title="Soy Candle", price="£18.00", description="Hand-poured, lavender scent")]

    app_config = AppConfig()
    line = draft_personal_line(app_config, "Acme Candles", products, client=client)

    assert line == "They sell hand-poured soy candles and reed diffusers."
    call_kwargs = client.messages.calls[0]
    assert call_kwargs["tool_choice"] == {"type": "auto"}
    prompt = call_kwargs["messages"][0]["content"]
    assert "Soy Candle" in prompt
    assert "No flattery" in prompt or "no flattery" in prompt.lower()


def test_draft_personal_line_raises_if_no_tool_call_returned():
    from tests.fake_anthropic import FakeTextBlock

    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("no tools for you")]) for _ in range(2)])
    app_config = AppConfig()
    with pytest.raises(RuntimeError):
        draft_personal_line(app_config, "Acme", [ProductResult(title="Candle", price="£10")], client=client)


def test_draft_personal_line_only_uses_given_products_not_invented_ones():
    client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "draft_personal_line", {"line": "ok"})])]
    )
    products = [ProductResult(title="Oak Chopping Board", price="£30")]
    app_config = AppConfig()

    draft_personal_line(app_config, "Acme Woodware", products, client=client)

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "Oak Chopping Board" in prompt
    assert "never invent" in prompt.lower()


# ---------- scrape_and_draft_store ----------
# The shared scrape+draft core — scripts.create_demo.build_demo (a sales
# demo) and app.live_onboarding.provision_live_store (an actual client,
# onboarded via OAuth) both call this and build their own flavour of YAML
# on top. See tests/test_create_demo.py for the full range of scraping
# scenarios (care page fallback, privacy/terms exclusion, etc.) — this
# only covers what's specific to the shared function itself.


class _FakeHttpResponse:
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
    monkeypatch.setattr("app.scraping.time.sleep", lambda seconds: None)


def _install_fake_routes(monkeypatch, routes: dict):
    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/robots.txt" and "/robots.txt" not in routes:
            return _FakeHttpResponse(status_code=404, text="")
        if path in routes:
            val = routes[path]
            return val(path) if callable(val) else val
        return _FakeHttpResponse(status_code=404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)


SHOPIFY_PRODUCTS_JSON = {
    "products": [
        {"title": "Soy Candle", "handle": "soy-candle", "body_html": "<p>Nice candle.</p>", "variants": [{"title": "Default Title", "price": "18.00", "available": True}]}
    ]
}


def test_scrape_and_draft_store_raises_for_non_shopify_store(monkeypatch):
    _install_fake_routes(monkeypatch, {"/products.json": _FakeHttpResponse(404, text="not found")})
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"
    with pytest.raises(StoreProvisioningError, match="doesn't look like a Shopify store"):
        scrape_and_draft_store("https://not-shopify.example", app_config)


def test_scrape_and_draft_store_raises_without_anthropic_key(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/": _FakeHttpResponse(200, text="<html><title>Acme</title></html>"), "/products.json": _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON)},
    )
    app_config = AppConfig()
    app_config.anthropic_api_key = ""
    with pytest.raises(StoreProvisioningError, match="ANTHROPIC_API_KEY"):
        scrape_and_draft_store("https://acme.example", app_config)


def test_scrape_and_draft_store_wraps_drafting_failure(monkeypatch):
    _install_fake_routes(
        monkeypatch,
        {"/": _FakeHttpResponse(200, text="<html><title>Acme</title></html>"), "/products.json": _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON)},
    )

    def _raise(*a, **k):
        raise RuntimeError("Claude is down")

    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", _raise)
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"
    with pytest.raises(StoreProvisioningError, match="failed to draft knowledge base"):
        scrape_and_draft_store("https://acme.example", app_config)


def test_scrape_and_draft_store_returns_expected_shape(monkeypatch):
    """No shipping/delivery page is fetched here at all (the fake routes
    only cover "/" and "/products.json"), so shipping_info_is_missing adds
    its own note to `missing` — this is correct, expected behaviour (a
    demo with literally no shipping info found should be flagged), not
    something this test should suppress. Pass a shipping-policy route
    with real delivery content instead, so the check doesn't fire and the
    original intent of this test (verify the returned shape, not
    re-verify shipping_info_is_missing — that has its own tests) still
    holds via a clean equality check.
    """
    _install_fake_routes(
        monkeypatch,
        {
            "/": _FakeHttpResponse(200, text="<html><title>Acme</title></html>"),
            "/products.json": _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON),
            "/policies/shipping-policy": _FakeHttpResponse(200, text="<p>We dispatch within 2 working days.</p>"),
        },
    )
    drafted = {"tone": "Friendly.", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []}
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", lambda *a, **k: drafted)
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"

    result = scrape_and_draft_store("https://acme.example", app_config)

    assert result["shop_name"] == "Acme"
    assert result["host"] == "acme.example"
    assert result["base_url"] == "https://acme.example"
    assert result["draft"] == {**drafted, "promotions": ""}
    assert len(result["products"]) == 1
    assert result["brand_color"]  # falls back to DEFAULT_BRAND_COLOR


def test_scrape_and_draft_store_grounds_brand_claims_against_the_homepage_too(monkeypatch):
    """Regression (real incident, 2026-10-28, Sassy Scents): the drafting
    prompt is explicitly told the homepage is a legitimate factual source
    for brand_claims (stats banners like "88% agree..." routinely only
    live there, nowhere else) — but scrape_and_draft_store was only
    passing the scraped POLICY pages (not homepage_text) into the
    grounding check, so every homepage-sourced brand claim got wrongly
    stripped as "unconfirmed," including real, correctly-quoted ones.
    """
    homepage_html = (
        "<html><title>Acme</title><body>Eliminates odour for up to 48 hours instead of masking it. "
        "88% agree Acme eliminated odour after 48 hours.</body></html>"
    )
    _install_fake_routes(
        monkeypatch,
        {"/": _FakeHttpResponse(200, text=homepage_html), "/products.json": _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON)},
    )
    drafted = {
        "tone": "Friendly.",
        "shipping": "",
        "returns": "",
        "sizing": "",
        "brand_claims": "Eliminates odour for up to 48 hours instead of masking it. 88% agree it eliminated odour after 48 hours.",
        "faq": [],
        "missing": [],
    }
    monkeypatch.setattr("app.demo_kb.draft_knowledge_base", lambda *a, **k: drafted)
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"

    result = scrape_and_draft_store("https://acme.example", app_config)

    assert result["draft"]["brand_claims"] == drafted["brand_claims"]
    assert not any("ungrounded" in note for note in result["draft"]["missing"])


def test_scrape_and_draft_store_forwards_client_to_draft_knowledge_base(monkeypatch):
    """scripts/provision_store.py passes a UsageTrackingClient through
    here so it can report real cost — must reach draft_knowledge_base."""
    _install_fake_routes(
        monkeypatch,
        {"/": _FakeHttpResponse(200, text="<html><title>Acme</title></html>"), "/products.json": _FakeHttpResponse(200, json_data=SHOPIFY_PRODUCTS_JSON)},
    )
    captured = {}
    drafted = {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": [], "missing": []}
    monkeypatch.setattr(
        "app.demo_kb.draft_knowledge_base",
        lambda app_config, shop_name, pages, homepage_text="", client=None: captured.update(client=client) or drafted,
    )
    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"
    sentinel = object()

    scrape_and_draft_store("https://acme.example", app_config, client=sentinel)

    assert captured["client"] is sentinel
