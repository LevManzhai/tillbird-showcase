"""Regression tests for real hallucinations caught during the 2026-10-24
to 2026-10-26 outreach prospecting rounds (see data/leads.csv — gitignored,
not in this repo, but the incident notes for each shop below were written
there at the time). Every case was first caught live by scripts/test_demo.py
against a real demo, patched on the spot with scripts/set_override.py, then
root-caused here:

- Babyblooms: asked "do you deliver outside the UK?", the demo invented
  international shipping prices and delivery windows that didn't match the
  real site ($25/£17.95/£12.95 vs the real $29.50/£19.95/£9.95).
- Make Memento: asked how long a personalised order takes, the demo
  invented "2-4 business days" for post-dispatch delivery (the real site
  says Royal Mail 1st Class, 1-2 working days) and separately claimed the
  shop ships internationally when it's UK-only.
- Hawthorn Handmade: asked the minimum age for a needle felting kit, the
  demo said "14+" when the site says "10 and over".
- Crouch End Candles: asked "do you offer candle making workshops?", the
  demo said it couldn't confirm — even though workshop sessions are live
  catalog products with a dedicated services page describing them.
- Callan Candles: asked "do you have a shop I can visit?", the demo named
  a real returns-only postal address as somewhere a customer could visit,
  contradicting the site's own "we are based in the West Country" FAQ
  answer and inventing a collection option nothing on the site states.

Two layers were fixed and are tested separately below:
1. app.agent.system_prompt — a stricter rule so the model itself refuses to
   state an unconfirmed number or yes/no, and checks search_products before
   denying a service/product-like offering exists.
2. app.scraping / app.demo_kb — the knowledge-base generator now also
   scrapes a services/workshops page, and a deterministic check flags when
   a shipping/delivery page was found but has no real delivery content
   (Crouch End Candles' case: the page returns 200 but is pure theme
   chrome), so a human knows to add an override before sending a demo out.

No test here makes a real Anthropic API call — every model interaction
uses tests.fake_anthropic.FakeAnthropicClient with a canned response.
"""
import requests

from app.agent.system_prompt import build_system_prompt
from app.config import AppConfig
from app.demo_kb import draft_knowledge_base
from app.scraping import PoliteFetcher, fetch_policy_texts, shipping_info_is_missing
from app.store_config import StoreConfig
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse, FakeToolUseBlock

BASE_STORE = {
    "store_id": "acme",
    "store_name": "Acme",
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
}


def _store(knowledge_base):
    return StoreConfig.model_validate({**BASE_STORE, "knowledge_base": knowledge_base})


# ---------------------------------------------------------------------------
# 1. System prompt tightening
# ---------------------------------------------------------------------------


def test_system_prompt_forbids_unconfirmed_numbers_and_yes_no_facts():
    store = _store({"shipping": "UK delivery: 1-2 working days.", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)

    assert "YES/NO answer about shipping destinations, age suitability, safety" in prompt
    assert "never estimate, round, average, infer, or give a plausible-sounding answer" in prompt
    # The two real failures that motivated this rule are named explicitly,
    # so a future edit that drops this language without replacing it with
    # something equally specific will be caught here.
    assert '"14+"' in prompt
    assert "invented a number for it anyway" in prompt


def test_system_prompt_requires_checking_search_products_before_denying_a_service():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)

    assert "search_products for it first" in prompt
    assert "candle making discovery session" in prompt.lower()
    assert "without ever searching" in prompt


def test_meta_assistant_system_prompt_does_not_reference_search_products_rule():
    """tillbird-site-style assistants have no product catalog at all —
    the search_products-before-denying rule only makes sense for a real
    shop (store.is_shop), so it must not appear for a non-shop assistant.
    """
    store = StoreConfig.model_validate(
        {**BASE_STORE, "store_id": "tillbird-site", "is_shop": False, "knowledge_base": {}}
    )
    prompt = build_system_prompt(store)
    assert "search_products for it first" not in prompt


# ---------------------------------------------------------------------------
# 2. shipping_info_is_missing — the deterministic "empty shipping page" flag
# ---------------------------------------------------------------------------


def test_shipping_info_is_missing_true_for_crouch_end_candles_case():
    """The real page: /policies/shipping-policy returns 200 and non-empty
    text, but that text is pure theme chrome (nav links, footer, payment
    icons) — no actual shipping information. policy_texts["shipping"] is
    therefore non-empty, so a plain truthiness check would miss this.
    """
    policy_texts = {
        "shipping": (
            "Shipping policy – crouch end candles Log in or Create account Cart 0 Search Menu "
            "Shop candles diffusers scented room sprays Shipping policy Latest News Links SEARCH "
            "ABOUT US TERMS OF SERVICE WHOLESALE REFUND & EXCHANGE POLICY GET IN TOUCH "
            "Powered by Shopify Payment icons American Express Apple Pay"
        ),
    }
    assert shipping_info_is_missing(policy_texts) is True


def test_shipping_info_is_missing_false_when_shipping_page_has_real_content():
    policy_texts = {"shipping": "We aim to dispatch within 1-2 working days via Royal Mail Tracked 48."}
    assert shipping_info_is_missing(policy_texts) is False


def test_shipping_info_is_missing_false_when_only_delivery_info_has_content():
    """delivery_info (a store's own dedicated page) is checked independently
    of shipping — a store can have real delivery content there even if the
    generic Shopify shipping-policy page is empty/unused.
    """
    policy_texts = {
        "shipping": "",
        "delivery_info": "Orders are dispatched within 2 working days via courier.",
    }
    assert shipping_info_is_missing(policy_texts) is False


def test_shipping_info_is_missing_true_when_no_shipping_keys_at_all():
    assert shipping_info_is_missing({}) is True
    assert shipping_info_is_missing({"returns": "30 days, unworn."}) is True


def test_scrape_and_draft_store_flags_missing_shipping_in_the_missing_list(monkeypatch):
    """End-to-end: scrape_and_draft_store must append the flag to
    draft["missing"] itself, not just leave it for a caller to remember to
    check shipping_info_is_missing() separately — scripts/create_demo.py's
    existing "Missing/TODO" print line (and batch_demos.py's quality check)
    already surface draft["missing"], so this is what actually gets a
    human's attention before a demo goes out.
    """
    from app.demo_kb import scrape_and_draft_store

    class FakeResp:
        def __init__(self, status_code=200, text="", json_data=None):
            self.status_code = status_code
            self.text = text
            self._json_data = json_data

        def json(self):
            if self._json_data is None:
                raise ValueError("no json")
            return self._json_data

    routes = {
        "/": FakeResp(200, text="<html><title>Crouch End Candles</title></html>"),
        "/products.json": FakeResp(200, json_data={"products": []}),
        "/policies/shipping-policy": FakeResp(
            200,
            text="<html><body>Shipping policy Latest News Links SEARCH ABOUT US Powered by Shopify</body></html>",
        ),
    }

    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/robots.txt":
            return FakeResp(status_code=404, text="")
        return routes.get(path, FakeResp(status_code=404, text=""))

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr("app.scraping.time.sleep", lambda seconds: None)

    fake_draft = {
        "tone": "Warm.",
        "shipping": "",
        "returns": "",
        "sizing": "",
        "brand_claims": "",
        "faq": [],
        "missing": [],
    }
    client = FakeAnthropicClient([FakeResponse("tool_use", [FakeToolUseBlock("t1", "draft_knowledge_base", fake_draft)])])

    app_config = AppConfig()
    app_config.anthropic_api_key = "sk-ant-test"
    result = scrape_and_draft_store("https://crouchendcandles.example", app_config, client=client)

    assert any("shipping" in note.lower() and "override" in note.lower() for note in result["draft"]["missing"])


# ---------------------------------------------------------------------------
# 3. Services/workshops page scraping (Crouch End Candles)
# ---------------------------------------------------------------------------


def test_fetch_policy_texts_captures_a_services_workshops_page(monkeypatch):
    workshop_text = (
        "Workshops and Events. SLOW SUNDAYS - CANDLE MAKING DISCOVERY SESSION. Come and learn about "
        "how your personal scent journey has influenced your life and pour your own custom candle "
        "with us in a sensory discovery session, hosted at our Hello Lovely Store, 83 Myddleton Road, "
        "Bowes Park, North London. £35 cover charge. We also run a Private Taper Candle Painting "
        "Workshop for 4-12 people."
    )

    class FakeResp:
        def __init__(self, status_code=200, text=""):
            self.status_code = status_code
            self.text = text

    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/robots.txt":
            return FakeResp(status_code=404, text="")
        if path == "/pages/workshops-and-events":
            return FakeResp(200, text=f"<html><body>{workshop_text}</body></html>")
        return FakeResp(status_code=404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr("app.scraping.time.sleep", lambda seconds: None)

    fetcher = PoliteFetcher("https://crouchendcandles.example")
    texts = fetch_policy_texts(fetcher)

    assert "services" in texts
    assert "SLOW SUNDAYS" in texts["services"]
    assert "Private Taper Candle Painting Workshop" in texts["services"]


def test_fetch_policy_texts_does_not_accept_a_services_page_with_no_real_offering(monkeypatch):
    """A page existing at one of the guessed URLs isn't enough on its own —
    same "keyword-gated" rule as care/size_guide/delivery_info/refund_info,
    so a nav label that happens to say "Events" over an unrelated blog
    listing doesn't get mistaken for a real workshops page.
    """

    class FakeResp:
        def __init__(self, status_code=200, text=""):
            self.status_code = status_code
            self.text = text

    def fake_get(self, url, timeout=None):
        from urllib.parse import urlparse

        path = urlparse(url).path
        if path == "/robots.txt":
            return FakeResp(status_code=404, text="")
        if path == "/pages/events":
            return FakeResp(200, text="<html><body>Latest news from our shop. Nothing bookable here.</body></html>")
        return FakeResp(status_code=404, text="")

    monkeypatch.setattr(requests.Session, "get", fake_get)
    monkeypatch.setattr("app.scraping.time.sleep", lambda seconds: None)

    fetcher = PoliteFetcher("https://example.com")
    texts = fetch_policy_texts(fetcher)

    assert "services" not in texts


# ---------------------------------------------------------------------------
# 4. draft_knowledge_base prompt construction — the real source text from
#    each live failure must actually reach the drafting prompt, so a future
#    scraper change can't silently drop the fact again.
# ---------------------------------------------------------------------------


def _fake_client():
    drafted = {
        "tone": "Warm.",
        "shipping": "placeholder",
        "returns": "",
        "sizing": "",
        "brand_claims": "",
        "faq": [],
        "missing": [],
    }
    return FakeAnthropicClient([FakeResponse("tool_use", [FakeToolUseBlock("t1", "draft_knowledge_base", drafted)])])


def _prompt_sent(client) -> str:
    return client.messages.calls[0]["messages"][0]["content"]


def test_babyblooms_international_shipping_source_text_reaches_the_prompt():
    client = _fake_client()
    scraped_pages = {
        "delivery_info": (
            "Do you ship outside of the UK? Yes, we do ship to certain countries outside of the UK. "
            "USA: Express International Delivery is available at a fixed cost of $29.50 USD, estimated "
            "delivery within 7-10 working days. EUROPE: EU Express Delivery (Excl ROI) £19.95, "
            "Republic of Ireland £9.95."
        ),
    }
    draft_knowledge_base(AppConfig(), "Babyblooms", scraped_pages, client=client)
    prompt = _prompt_sent(client)

    for figure in ("$29.50", "7-10 working days", "£19.95", "£9.95"):
        assert figure in prompt


def test_make_memento_delivery_and_international_source_text_reaches_the_prompt():
    client = _fake_client()
    scraped_pages = {
        "delivery_info": (
            "Delivery Locations: We currently ship to UK addresses only. Unfortunately, we are unable "
            "to deliver to the Channel Islands or the Isle of Man. Production Times: all orders are "
            "dispatched within 2 weeks at the latest. Delivery Times Following Dispatch: once "
            "dispatched, Royal Mail 1st Class typically delivers within 1-2 working days."
        ),
    }
    draft_knowledge_base(AppConfig(), "Make Memento", scraped_pages, client=client)
    prompt = _prompt_sent(client)

    assert "UK addresses only" in prompt
    assert "2 weeks at the latest" in prompt
    assert "Royal Mail 1st Class typically delivers within 1-2 working days" in prompt


def test_hawthorn_handmade_minimum_age_source_text_reaches_the_prompt():
    client = _fake_client()
    scraped_pages = {
        "faq": (
            "What age are your needle felting kits suitable for? As a general rule we recommend that "
            "the kits are for children aged 10 and over due to the sharp needles."
        ),
    }
    draft_knowledge_base(AppConfig(), "Hawthorn Handmade", scraped_pages, client=client)
    prompt = _prompt_sent(client)

    assert "aged 10 and over" in prompt
    assert "14" not in prompt.split("aged 10 and over")[0][-50:]  # sanity: no stray wrong age nearby


def test_crouch_end_candles_services_source_text_reaches_the_prompt():
    client = _fake_client()
    scraped_pages = {
        "services": (
            "SLOW SUNDAYS - CANDLE MAKING DISCOVERY SESSION. Pour your own custom candle with us, "
            "hosted at our Hello Lovely Store, Bowes Park, North London."
        ),
    }
    draft_knowledge_base(AppConfig(), "Crouch End Candles", scraped_pages, client=client)
    prompt = _prompt_sent(client)

    assert "[services]" in prompt
    assert "CANDLE MAKING DISCOVERY SESSION" in prompt
    assert "brand_claims" in prompt  # the prompt instructs folding services into brand_claims


def test_callan_candles_based_in_west_country_source_text_reaches_the_prompt():
    client = _fake_client()
    scraped_pages = {
        "faq": "Where are you based? We are based in the West Country.",
    }
    draft_knowledge_base(AppConfig(), "Callan Candles", scraped_pages, client=client)
    prompt = _prompt_sent(client)

    assert "We are based in the West Country" in prompt
