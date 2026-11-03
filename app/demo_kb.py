"""Draft a store's knowledge base + tone from scraped policy text using Claude.

Strict rule: only use what's actually stated in the source text (homepage,
scraped policy pages, and an About/Our Story/FAQ/services-workshops page if
one was found).
Anything not covered is left blank and listed in `missing` for the human to
fill in — never guessed, never filled with typical/generic policy text, and
never padded with a plausible-sounding extra condition the source text
doesn't actually state (e.g. "unworn, with tags" when the policy just says
"unworn"). The homepage is a legitimate factual source (the store's own
public claims), but a scraped policy page wins if the two conflict, and
promotional claims (sales, discount codes, limited offers) must be framed as
"check at checkout" rather than stated as an ongoing fact.

Brand/product claims (pet safety, ingredients, materials, vegan, handmade,
made in the UK, sustainability, etc.) are drafted into their own
`brand_claims` field, separate from shipping/returns/sizing, since they're
the store's claims about itself rather than policy — see KnowledgeBase in
app.store_config. Garment care/washing instructions go into `sizing`
alongside fit guidance, since they're commonly scraped from the same page
and answered in the same breath — see app.scraping.fetch_product_care_text
for where care text often actually lives (a product page's "Garment Care"
section, not any dedicated page).

A store's own dedicated delivery/returns page ("[delivery_info]"/
"[refund_info]" in the scraped text, if found — see
app.scraping.DELIVERY_PATH_CANDIDATES/REFUND_PATH_CANDIDATES) is more
authoritative than Shopify's generic policy page or the homepage for
shipping/returns facts, and figures for the same fact (e.g. dispatch days
and price) must always come from one single source, never mixed across
pages — confirmed live: a stale "2-3 working days" homepage FAQ answer won
over the store's own current "1-2 working days" delivery page simply
because the delivery page wasn't being scraped at all.
"""

import json
from urllib.parse import urlparse

import anthropic

from app.kb_grounding import ground_knowledge_base
from app.llm import create_with_tool
from app.scraping import (
    PoliteFetcher,
    detect_brand_color,
    detect_shop_name,
    fetch_all_products,
    fetch_homepage,
    fetch_policy_texts,
    fetch_product_care_text,
    is_password_protected,
    is_shopify_store,
    normalise_base_url,
    shipping_info_is_missing,
    strip_html_to_text_keep_banner,
)

DEFAULT_BRAND_COLOR = "#1a1a2e"

DRAFT_KB_TOOL = {
    "name": "draft_knowledge_base",
    "description": "Return the drafted customer-support knowledge base for this store.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tone": {
                "type": "string",
                "description": "One-line tone description inferred from the site's voice, e.g. 'Warm, concise and helpful.'",
            },
            "shipping": {"type": "string", "description": "Shipping policy summary, or empty string if not stated"},
            "returns": {"type": "string", "description": "Returns/refund policy summary, or empty string if not stated"},
            "sizing": {
                "type": "string",
                "description": (
                    "Sizing/fit guidance AND garment care/washing instructions (e.g. 'hand wash only', "
                    "'dry clean recommended', 'do not tumble dry'), or empty string if neither is stated"
                ),
            },
            "brand_claims": {
                "type": "string",
                "description": (
                    "The store's own stated claims about itself/its products — pet or child safety, "
                    "ingredients/materials, vegan/cruelty-free, handmade, made in the UK, sustainability, "
                    "allergen info, etc. — AND any in-person services/experiences it offers (workshops, "
                    "classes, booked sessions, events) — drawn from the homepage and any about/story/FAQ/"
                    "services page. Empty string if the source text doesn't actually state any of this."
                ),
            },
            "faq": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                    "required": ["question", "answer"],
                },
                "description": "FAQ entries drawn directly from the source text (e.g. contact info, response times)",
            },
            "missing": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short notes on things a support bot would need that weren't found in the source text",
            },
        },
        "required": ["tone", "shipping", "returns", "sizing", "brand_claims", "faq", "missing"],
    },
}

# A current store-wide multi-buy/bundle/sale deal (e.g. "5 for £15") is
# built directly from app.scraping's extracted phrases, NOT asked of the
# drafting LLM above — confirmed live, twice, that no amount of prompt
# wording reliably stopped Claude from padding in a second, higher-quantity
# tier ("10 for £25") that appears nowhere in the scraped text, presumably
# because that's a common real-world pattern for this kind of promo and
# the model treats it as an obvious extrapolation rather than an invention.
# The only figures that can appear here are therefore ones that literally
# came out of the regex match in app.scraping.fetch_policy_texts.
_PROMOTIONS_TEMPLATE = (
    "There's a current deal on the site: {phrases}. Worth confirming the exact terms and which "
    "products are eligible at checkout, since promotions can change."
)


def draft_promotions(policy_texts: dict[str, str]) -> str:
    """Builds the `promotions` knowledge-base field directly from the
    phrases app.scraping.fetch_policy_texts extracted into
    policy_texts["promotions"] (if any) — see _PROMOTIONS_TEMPLATE for why
    this is template text, not an LLM paraphrase.
    """
    phrases = policy_texts.get("promotions", "")
    return _PROMOTIONS_TEMPLATE.format(phrases=phrases) if phrases else ""

_PROMPT_TEMPLATE = """You are drafting a customer-support knowledge base for a UK Shopify store, from real \
scraped text off their own site. This will be used verbatim by an AI support agent talking to real customers.

CRITICAL: only use facts explicitly stated in the source text below (homepage or scraped pages). Do not \
invent, guess, or fill in "typical" policy details (e.g. delivery times, carriers, return windows) that \
aren't actually stated anywhere. Never add a condition or qualifier that sounds plausible but isn't \
literally stated — e.g. if the returns policy says "unworn" but never mentions tags or original packaging, \
write "unworn" only, don't add "with tags attached" or "in original packaging" because that's a common \
condition for this kind of store. If a section has nothing relevant in the source text, return an empty \
string (or empty list for faq) for it, and add a short note to `missing` explaining what's missing and why \
a support bot would need it.

STRICT RULE for every NUMBER, PRICE, TIME RANGE, PERCENTAGE and CARRIER/SERVICE NAME you write (e.g. "2-3 \
working days", "£4.50", "10%", "Royal Mail Tracked 48"): copy the figure verbatim from the source text — \
never round, average, split a range, invent a plausible-sounding number, or name a carrier/courier that \
isn't literally named in the source. If the source states a range as "3 to 5 working days", write it that \
way (or as "3-5 working days" — the two numbers, not different ones); don't shorten or adjust it to a \
different-sounding range. If no carrier is named anywhere in the source, don't name one — say "a tracked \
courier service" or similar, never invent "Royal Mail", "DPD", "Evri" or any other specific name. The same \
applies to unconditional-sounding qualifiers ("regardless of order value", "always free", "on every order") \
— only include one of these if the source states that exact scope, never because it reads naturally after a \
real fact. When in doubt, prefer quoting the source sentence closely rather than paraphrasing a number or \
named detail into new wording — paraphrasing a number is how a wrong one slips in. Confirmed live: a demo's \
shipping text invented "Royal Mail Tracked 48" and "2-3 working days" for a site whose real FAQ names no \
carrier at all and says "3 to 5 working days", and separately invented "regardless of value" for a free- \
delivery claim the site never qualified that way. (A second, deterministic check also runs after this \
drafting step and will strip out anything that still isn't grounded in the source text, so don't rely on \
that catching it — get it right here.)

The homepage text is a legitimate source of facts, not just a tone sample — it's the store's own public \
claims about itself (location, materials, what shops stock them, etc.). If the homepage and a scraped \
policy page state something differently, the policy page is authoritative — use that instead.

AUTHORITY ORDER for shipping/delivery facts (dispatch time, price, carrier) and returns facts (window, \
conditions, process), most to least authoritative:
1. "[delivery_info]" / "[refund_info]" — a store's own dedicated delivery/returns page, if present below.
2. "[shipping]" / "[returns]" — Shopify's generic policy page.
3. A product page's own description, if the fact happens to be stated there.
4. The homepage, including any banner or FAQ on it.
"[delivery_info]"/"[refund_info]" is MORE authoritative than every other source listed above — confirmed \
live on a real store whose homepage FAQ \
stated a stale "2-3 working days" while its own dedicated delivery page said "1-2 working days" (the correct, \
current figure), and separately confirmed on a store where the homepage blurb said "2-3 working days" while \
the delivery page AND every product page agreed on "1-2 working days". If "[delivery_info]" is present, draft \
`shipping` entirely from it, not the homepage, a product page, or "[shipping]" — same for "[refund_info]" and \
`returns`. NEVER MIX figures from different sources for the same fact — e.g. don't take the day-count from \
one source and the price/carrier from another just because both mention shipping; pick the single most \
authoritative source that has the fact and quote everything related to it from that one source together.

This authority order is for picking a winner when two sources DISAGREE about the same fact — it does NOT mean \
ignoring a genuinely separate delivery or current-offer fact that only the homepage/banner states, which none \
of the higher-priority sources mention in any form at all (confirmed live: a homepage banner reading "NEXT DAY \
DELIVERY — NOW AVAILABLE ON OUR BEST SELLING HEDGEHOG HOUSE AND BIRD TABLE" was simply dropped because the \
dedicated delivery page didn't mention next-day delivery either way, not because it conflicted with anything). \
If the homepage states something like that — a specific, current delivery option or offer that is ABSENT \
(not contradicted) from "[delivery_info]"/"[shipping]"/every product page — add one short sentence for it at \
the end of `shipping`, clearly marked as a secondary, less certain addition rather than the main stated policy, \
e.g. "Also stated on the site: next-day delivery is available on the Hedgehog House and Bird Table." Only do \
this when the higher-priority sources are silent on that SPECIFIC fact; if any of them states something that \
actually conflicts with it, the authority order above still wins and the homepage claim is dropped entirely, \
not flagged.

`brand_claims`: pull together the store's own stated claims about itself and its products — pet or child \
safety, ingredients/materials, vegan or cruelty-free, handmade, made in the UK, sustainability, allergen \
info, etc. — from the homepage and any about/story/FAQ page in the scraped text below. These are the \
store's own claims, not policy, so they belong here even without a dedicated policy page covering them. \
Same rule as everywhere else: only what's actually stated, nothing inferred or assumed typical for this \
kind of product (e.g. don't assume "handmade" or "vegan" just because the product category often is).

Also fold into `brand_claims` any in-person services/experiences the store states it offers — workshops, \
classes, booked sessions, parties, events — look for these in a "[services]" section below if present, as \
well as the homepage/about/FAQ text. Confirmed live: a candle shop sells its workshops as ordinary catalog \
products (so a support bot can find them via product search), but it ALSO had a dedicated page describing \
what the sessions actually involve, where, and the cost — that descriptive detail belongs here since \
product search alone won't surface it. If nothing in the source text mentions any such offering, leave it \
out — don't assume a store offers workshops/classes just because similar shops often do.

`sizing` also covers garment care/washing instructions (hand wash, dry clean, tumble dry, ironing, etc.), \
not just fit/sizing guidance (measurements, "runs small/large", a size chart) — look for either in the \
"[care]" section below (a dedicated care page, or a snippet pulled from a real product page's care section, \
since this is often only written there) and the "[size_guide]" section (a dedicated sizing/measurement \
page). Combine whichever of sizing/care are present into one paragraph; if only one is stated, include just \
that one. If a size_guide section has an actual measurement chart, include a few real figures from it (e.g. \
"a size M is about 91cm bust, 76cm waist, 103cm hips") rather than just telling the customer to go check the \
size guide on the website — the whole point of this field is to let the support bot answer the question \
directly, not redirect somewhere else. Only redirect to "check the size guide" as a last resort if the \
source text is a chart too large/detailed to usefully summarise.

Promotional claims (a sale, a discount code, a "limited time" offer) must never be stated as an ongoing \
fact, since they go stale — phrase them as pointing the customer to check the current offer at checkout, \
e.g. "there may be a seasonal discount running — check at checkout for the current offer" rather than \
"get 20% off this month."

A "[promotions]" section, if present below, is informational only — ignore it when drafting every other \
field above; it's turned into its own `promotions` entry separately, by code, not by you (see \
app.demo_kb.draft_promotions), specifically because asking an LLM to restate its exact figures has proven \
unreliable (it tends to pad in a second, plausible-sounding tier that isn't actually stated).

Write shipping/returns/sizing/brand_claims as flowing prose paragraphs a support bot can read aloud to a \
customer, not a bullet list or a copy-paste of legal text. Keep each under ~150 words.

Store name: {store_name}
Store homepage text (a real factual source — see rules above for how to use it): {homepage_text}

=== Scraped page text (policy, about/story, services/workshops, care/size-guide, delivery/refund-info and FAQ pages — authoritative over the homepage if they conflict; see AUTHORITY ORDER above for delivery_info/refund_info specifically) ===
{scraped_text}
=== End scraped text ===
"""


def draft_knowledge_base(
    app_config, store_name: str, scraped_pages: dict[str, str], homepage_text: str = "", client=None
) -> dict:
    """scraped_pages: dict of page-key -> page text, from app.scraping.fetch_policy_texts.
    Returns the tool's input dict: tone, shipping, returns, sizing, brand_claims, faq, missing.
    """
    scraped_text = "\n\n".join(f"[{key}]\n{text}" for key, text in scraped_pages.items()) or "(no pages found)"
    prompt = _PROMPT_TEMPLATE.format(
        store_name=store_name,
        # Real homepages can bury a brand claim (e.g. a "vegan, pet-safe"
        # badge row) well past the first couple thousand characters behind
        # boilerplate _LOCALE_SELECTOR_RE doesn't catch — generous enough
        # that truncation itself stops being the thing that loses a claim.
        homepage_text=homepage_text[:6000] or "(not available)",
        scraped_text=scraped_text[:16000],
    )

    client = client or anthropic.Anthropic(api_key=app_config.anthropic_api_key)
    response = create_with_tool(
        client,
        tool_name="draft_knowledge_base",
        model=app_config.anthropic_model,
        # Confirmed live: a verbose store (long shipping/returns/brand_claims
        # prose) hit the old 2000-token cap mid-response, silently truncating
        # the tool call before it ever reached faq/promotions/missing — the
        # demo then answered as if no promotion existed at all, rather than
        # erroring. Then confirmed live again (Pott Candles, 2026-10-30): a
        # large catalogue hit 3000 too, twice in a row. 6000 is only billed
        # when actually used; the stop_reason check below stays the safety
        # net, so a truncated draft is never mistaken for a complete one.
        max_tokens=6000,
        tools=[DRAFT_KB_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )

    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            "draft_knowledge_base response was truncated (stop_reason=max_tokens) — "
            "a partial tool call would silently drop whichever fields Claude hadn't "
            "written yet (e.g. a real promotion reported as absent), so this must "
            "error rather than be treated as a complete draft."
        )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "draft_knowledge_base":
            return block.input

    raise RuntimeError(f"Claude did not return a draft_knowledge_base tool call: {json.dumps([str(b) for b in response.content])}")


DRAFT_PERSONAL_LINE_TOOL = {
    "name": "draft_personal_line",
    "description": "Return one short, natural sentence about this store for a cold outreach email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "line": {
                "type": "string",
                "description": "One short, natural sentence (under ~20 words) about the store, grounded "
                "only in the product titles/descriptions given — no flattery, no invented facts.",
            },
        },
        "required": ["line"],
    },
}

_PERSONAL_LINE_PROMPT_TEMPLATE = """You're writing ONE short sentence for a cold outreach email to a UK shop \
owner, about their own store, to show we actually looked at it rather than sending a generic mass email.

CRITICAL RULES:
- Use ONLY the real product titles/descriptions listed below — never invent a product, material, feature, \
or claim that isn't there.
- No flattery or generic marketing adjectives ("amazing", "stunning", "beautiful", "incredible").
- No fake specificity (don't guess at order volume, customer counts, years in business, etc. unless given).
- Plain, natural, matter-of-fact tone, as if a real person briefly looked at the shop. Under 20 words.
- Write it as a sentence fragment that could follow "I had a look at your store and noticed..." — don't \
include that lead-in itself, just the fact.

Store name: {store_name}
Real products (the only facts you may use):
{product_listing}
"""


def draft_personal_line(app_config, store_name: str, products: list, client=None) -> str:
    """products: list of app.catalog.ProductResult (or anything with .title/.description).
    Returns a short, strictly-grounded sentence for use as {{personal_line}} in cold outreach.
    """
    sample = products[:12]
    product_listing = (
        "\n".join(f"- {p.title}" + (f": {p.description[:120]}" if getattr(p, "description", "") else "") for p in sample)
        or "(no products available)"
    )
    prompt = _PERSONAL_LINE_PROMPT_TEMPLATE.format(store_name=store_name, product_listing=product_listing)

    client = client or anthropic.Anthropic(api_key=app_config.anthropic_api_key)
    response = create_with_tool(
        client,
        tool_name="draft_personal_line",
        model=app_config.anthropic_model,
        max_tokens=200,
        tools=[DRAFT_PERSONAL_LINE_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "draft_personal_line":
            return (block.input.get("line") or "").strip()

    raise RuntimeError(f"Claude did not return a draft_personal_line tool call: {json.dumps([str(b) for b in response.content])}")


class StoreProvisioningError(Exception):
    """Raised by scrape_and_draft_store for conditions that should stop
    provisioning right there (password-protected, not a Shopify store, no
    Anthropic key, the draft call itself failing) — shared by
    scripts.create_demo.build_demo (a sales demo; re-raises this as its
    own DemoBuildError so every existing caller/test keeps working
    unchanged) and app.live_onboarding.provision_live_store (an actual
    client, onboarded via the OAuth install flow).
    """


def scrape_and_draft_store(store_url: str, app_config, client=None) -> dict:
    """The part a sales demo and a real client's auto-provisioned live
    store share exactly: scrape store_url's public storefront and draft
    its knowledge base with Claude, stopping before either writes its own
    flavour of stores/<id>.yaml (mode: demo vs mode: live).

    `client` is forwarded to draft_knowledge_base — pass a
    app.anthropic_usage.UsageTrackingClient to report what a run actually
    spent (scripts/provision_store.py does this); defaults to a plain
    anthropic.Anthropic client otherwise.

    Returns {"base_url", "host", "shop_name", "brand_color", "products",
    "policy_texts", "draft"}. Raises ScrapeError (bad URL) or
    StoreProvisioningError (password-protected / not Shopify / no
    Anthropic key / drafting failed).
    """
    base_url = normalise_base_url(store_url)
    fetcher = PoliteFetcher(base_url)

    if is_password_protected(fetcher):
        raise StoreProvisioningError(
            f"{base_url} looks password-protected (no public access) — "
            "can't provision a store that isn't publicly browsable."
        )
    if not is_shopify_store(fetcher):
        raise StoreProvisioningError(
            f"{base_url} doesn't look like a Shopify store — no public /products.json found."
        )

    homepage_soup = fetch_homepage(fetcher)
    # keep_banner, not the plain strip — an announcement bar ("5 FOR £15 MIX
    # & MATCH", "FREE DELIVERY OVER £30") is usually nested inside
    # <header>/<nav>, which the plain strip removes as chrome (see
    # app.audit's identical use, for the same reason); losing it here meant
    # a promo stated only in the banner was invisible to the demo generator.
    homepage_text = strip_html_to_text_keep_banner(str(homepage_soup)) if homepage_soup else ""
    shop_name = detect_shop_name(homepage_soup, base_url)
    brand_color = detect_brand_color(homepage_soup) or DEFAULT_BRAND_COLOR

    products = fetch_all_products(fetcher)
    policy_texts = fetch_policy_texts(fetcher, homepage_soup=homepage_soup)
    if "care" not in policy_texts:
        product_care_text = fetch_product_care_text(fetcher, products)
        if product_care_text:
            policy_texts["care"] = product_care_text

    if not app_config.anthropic_api_key:
        raise StoreProvisioningError("ANTHROPIC_API_KEY is not set (needed to draft the knowledge base)")

    # privacy/terms-of-service text is only ever used for the
    # privacy_policy_url presence check, never for drafting — see
    # build_demo's own original note on this (scripts/create_demo.py).
    draft_input_pages = {k: v for k, v in policy_texts.items() if k not in ("privacy", "terms")}
    try:
        draft = draft_knowledge_base(app_config, shop_name, draft_input_pages, homepage_text=homepage_text, client=client)
    except Exception as exc:
        raise StoreProvisioningError(f"failed to draft knowledge base: {exc}") from exc

    # Deterministic, no-API-call second pass: strip any number/price/time
    # range/percentage/carrier name the drafting prompt above still got
    # wrong — see app.kb_grounding for why a prompt instruction alone
    # isn't trusted to catch this on its own (confirmed live, in
    # production, after the prompt above already said not to do this).
    # The homepage is included here too — it's a legitimate source the
    # drafting prompt is explicitly told it may cite (brand_claims in
    # particular routinely comes from homepage banner stats, e.g. "88%
    # agree..."), so grounding must check against it as well, not only
    # the scraped policy/about/FAQ pages, or every homepage-sourced claim
    # gets wrongly stripped as unconfirmed (confirmed live: Sassy Scents'
    # real homepage percentage stats were removed this way).
    grounding_source_pages = {**draft_input_pages, "homepage": homepage_text}
    draft = ground_knowledge_base(draft, grounding_source_pages)

    # Built by code from the raw scraped phrases, not drafted by the LLM
    # above — see draft_promotions for why.
    draft["promotions"] = draft_promotions(policy_texts)

    # A deterministic, code-level check (not left to the drafting LLM to
    # notice) — see app.scraping.shipping_info_is_missing for why this
    # can't just rely on policy_texts["shipping"] being non-empty. Surfaced
    # via the same `missing` list a human already checks (printed by
    # scripts/create_demo.py / scripts/batch_demos.py) so a demo that will
    # have nothing real to say about shipping gets flagged for a manual
    # override (scripts/set_override.py) rather than going out silently.
    if shipping_info_is_missing(policy_texts):
        draft["missing"] = list(draft.get("missing") or []) + [
            "No real shipping/delivery information found on the site (the shipping policy page is "
            "empty or just theme chrome) — add a manual override with scripts/set_override.py before "
            "sending this demo out."
        ]

    return {
        "base_url": base_url,
        "host": urlparse(base_url).netloc,
        "shop_name": shop_name,
        "brand_color": brand_color,
        "products": products,
        "policy_texts": policy_texts,
        "draft": draft,
    }
