from app.agent.system_prompt import build_system_prompt
from app.agent.tools import build_tool_schemas
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
}


def _store(knowledge_base):
    return StoreConfig.model_validate({**BASE_STORE, "knowledge_base": knowledge_base})


def test_small_kb_is_inlined_in_system_prompt_and_omits_get_policy_tool():
    store = _store(
        {
            "shipping": "Royal Mail, 2-3 days.",
            "returns": "30 days, unworn.",
            "sizing": "True to size.",
            "faq": [{"question": "Do you ship to Ireland?", "answer": "Yes, via Evri."}],
        }
    )
    prompt = build_system_prompt(store)
    assert "Royal Mail, 2-3 days." in prompt
    assert "30 days, unworn." in prompt
    assert "Do you ship to Ireland?" in prompt
    assert "Yes, via Evri." in prompt

    tool_names = [t["name"] for t in build_tool_schemas(store)]
    assert "get_policy" not in tool_names
    assert "search_products" in tool_names


def test_brand_claims_are_inlined_in_small_kb_system_prompt():
    """Regression: a customer asked The Candle Brand's demo "are your
    candles safe to burn around my dog?" and got "I don't have that
    information", even though the store's homepage/About page says exactly
    that — the knowledge base had no field for brand/product claims at all,
    so draft_knowledge_base had nowhere to put it. brand_claims must be
    inlined alongside shipping/returns/sizing.
    """
    store = _store(
        {
            "shipping": "",
            "returns": "",
            "sizing": "",
            "brand_claims": "All candles are made with pet- and family-safe, phthalate-free fragrance oils.",
            "faq": [],
        }
    )
    prompt = build_system_prompt(store)
    assert "pet- and family-safe" in prompt
    assert "brand/product-claim" in prompt.lower() or "brand & product claims" in prompt.lower()


def test_large_kb_is_not_inlined_and_keeps_get_policy_tool():
    huge_text = "Delivery details. " * 3000  # comfortably over the ~8k token threshold
    store = _store({"shipping": huge_text, "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert huge_text not in prompt
    assert "too large to include" in prompt

    tool_names = [t["name"] for t in build_tool_schemas(store)]
    assert "get_policy" in tool_names
    assert "search_products" in tool_names


def test_empty_kb_is_treated_as_small_with_no_tool_needed():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "no knowledge base configured" in prompt
    tool_names = [t["name"] for t in build_tool_schemas(store)]
    assert "get_policy" not in tool_names


def test_prompt_never_names_a_carrier_unless_the_kb_does():
    """Regression: the prompt used to hardcode example UK carrier names
    (Royal Mail, Evri, DPD, DHL, Parcelforce) as general vocabulary, and the
    model would cite them as if they were this store's actual courier even
    when the knowledge base deliberately didn't name one. The prompt itself
    must not introduce any carrier name that isn't in the store's own KB.
    """
    store = _store({"shipping": "Tracked delivery; the carrier can vary.", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    for carrier in ("Royal Mail", "Evri", "DPD", "DHL", "Parcelforce"):
        assert carrier not in prompt, f"prompt introduced unconfigured carrier name {carrier!r}"


def test_prompt_instructs_synonym_search_for_allergy_questions():
    """Regression: asked about a citrus allergy, the agent only searched the
    literal word "citrus" and named a single matching product, missing other
    products whose citrus note (e.g. lime) used a different word — implying
    the rest of the range was safe by omission. The prompt must instruct
    searching known synonyms and listing every match, not just one example.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "allergy" in prompt.lower()
    for synonym in ("lime", "lemon", "bergamot", "grapefruit", "neroli"):
        assert synonym in prompt.lower()
    assert "ingredient lists may be incomplete" in prompt.lower()
    assert "list every matching product" in prompt.lower()
    # Regression: telling the model to issue one tool call per synonym burned
    # enough output tokens on tool-call JSON to truncate the actual reply
    # (see test_run_agent_turn_falls_back_when_response_has_no_text). It must
    # be told to combine synonyms into a single comma-separated search instead.
    assert "one combined search" in prompt.lower()
    assert "rather than a separate call per synonym" in prompt.lower()


def test_prompt_instructs_speaking_as_the_store_in_first_person():
    """Regression: The Candle Brand's demo answered "The Candle Brand
    states that their products are Family + Pet Safe" — describing the
    store from the outside rather than speaking as it. Must be told to
    use "we"/"our" and never describe the store's own claims in third
    person.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "first person" in prompt.lower()
    assert '"we", "our", "us"' in prompt or "we\", \"our\", \"us\"" in prompt
    assert "Acme Fashion states that their" in prompt  # the bad example is quoted so the model recognises it to avoid


def test_prompt_instructs_sizing_search_fallback_before_saying_no_info():
    """Regression: Nights by Wilder's demo said it had no size info for
    "what size for a 5 year old?" even though age-range sizing is stated
    per product (e.g. "age 2-4 up to 10-12") rather than on any general
    sizing page — the model must search_products and check descriptions
    before giving up, not just rely on the knowledge-base sizing field.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "search_products for a relevant term" in prompt
    assert "age" in prompt.lower()
    assert "don't claim there's no sizing information at all without having checked" in prompt.lower()


def test_prompt_forbids_exposing_internal_information_limits():
    """Regression: the live agent said things like "I don't have a
    specific runs-small verdict in our records" and "I don't have that
    on hand" — exposing its own information architecture instead of
    speaking like a normal team member. When a relevant page is known
    (e.g. a size guide), it must point the customer there by name rather
    than describing what it does or doesn't have.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "in my records" in prompt.lower() or "not in my records" in prompt.lower()
    assert "on hand" in prompt.lower()
    assert "point the customer straight to it by name" in prompt.lower() or "point the customer straight to it" in prompt.lower()


def test_prompt_fit_verdict_rule_points_to_size_guide_instead_of_stating_lack_of_info():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "point them to it for the exact fit" in prompt.lower()
    assert "rather than stating that you lack a verdict" in prompt.lower()


def test_prompt_forbids_inventing_fit_verdicts():
    """Regression: asked "do your t-shirts run true to size?", the live
    agent answered "we design our t-shirts with a flattering, true-to-size
    fit" — a specific fit verdict the store never actually stated (the
    knowledge base only had a general measurement chart, no runs-small/
    true-to-size claim at all). Must never invent a yes/no fit verdict.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "fit verdict" in prompt.lower()
    assert "true to size" in prompt.lower()
    assert "runs small/large" in prompt.lower() or "run small/large" in prompt.lower()


def test_prompt_forbids_resolving_an_ambiguous_return_exclusion_in_the_customers_favour():
    """Regression (the actual live bug): CharlartsCrafts' demo correctly
    recited "personal care goods ... can't be returned" and then, in the
    SAME reply, still told the customer "you're welcome to return it" for
    a wax melt — the kind of item that exclusion plausibly covers. The
    model must treat an ambiguous classification as excluded, not resolve
    it in the customer's favour, and hand off rather than guess when the
    policy is genuinely silent on the exact case.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "refund, return, exchange, delivery-time and warranty questions" in prompt.lower()
    assert "literal quoted source" in prompt.lower()
    assert "personal care goods" in prompt.lower()
    assert "you're welcome to return it" in prompt.lower()
    assert "do not resolve" in prompt.lower() or "don't resolve" in prompt.lower() or "do not resolve that ambiguity" in prompt.lower()
    assert "call handoff_to_human" in prompt.lower()


def test_prompt_forbids_adding_unstated_conditions():
    """Regression: a returns-policy answer said "unworn, with tags" for a
    store whose actual policy only said "unworn" — the model padded in a
    plausible-sounding extra condition that wasn't actually stated anywhere.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "tags" in prompt.lower()
    assert "never add an extra condition" in prompt.lower()


def test_prompt_forbids_stating_bundle_price_as_per_item():
    """Regression: the "Fresh & Clean Clamshell Bundle" (5 clamshells for
    £20 total, a single-variant product) got described as "pick 5
    clamshells for £20 each" — overstating the price 5x. The prompt must
    instruct stating a bundle's one price as the price for the whole
    bundle, not per item.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "bundle" in prompt.lower()
    assert '"5 for £20"' in prompt or "5 for £20" in prompt
    assert "never" in prompt.lower() and "each" in prompt.lower()


def test_prompt_forbids_doing_its_own_price_maths():
    """B2 spec: beyond the bundle-specific rule above, the model must never
    compute a per-unit price, a multi-item total, a discount, or a
    delivery-inclusive total on its own — only ever quote a figure that's
    actually stated.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "never do your own price maths" in prompt.lower()
    assert "per-unit price" in prompt.lower()
    assert "discount amount" in prompt.lower()
    assert "delivery-inclusive total" in prompt.lower()


def test_prompt_requires_vet_line_and_forbids_treatment_claims_for_pet_symptoms():
    """A customer describing a pet's symptom (itching, vomiting, limping)
    must get a vet referral, never a diagnosis or a product offered as the
    treatment — plain product info is still fine if it's actually stated.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "itching" in prompt.lower()
    assert "vomiting" in prompt.lower()
    assert "limping" in prompt.lower()
    assert "never diagnose" in prompt.lower()
    assert "never recommend or imply a product as a treatment" in prompt.lower()
    assert "vet" in prompt.lower()
    assert "if it persists or seems serious" in prompt.lower()


def test_prompt_offers_handoff_when_pet_problem_started_after_a_product():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "started after using one of our products" in prompt.lower()
    assert "also offer a handoff" in prompt.lower()


def test_prompt_requires_gp_or_pharmacist_line_for_human_health_and_forbids_treatment_claims():
    """A customer's own health question (allergy, pregnancy, skin reaction,
    medical condition) must only get what the site actually states, a
    GP/pharmacist referral, and a handoff offer — never a "safe for X"
    claim the site doesn't make in those words, and never a treats/cures
    claim, same as the pet-health rule.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "pregnant" in prompt.lower()
    assert "skin reaction" in prompt.lower()
    assert "medical condition" in prompt.lower()
    assert "gp or pharmacist" in prompt.lower()
    assert '"safe for"' in prompt.lower() or "safe for" in prompt.lower()
    assert "unless the site says so in those exact words" in prompt.lower()
    assert "offer a handoff" in prompt.lower()
    assert "never claim or imply a product treats, cures, or is medically effective" in prompt.lower()


def test_prompt_instructs_varying_wording_between_replies():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "twice in a row" in prompt.lower()


def test_prompt_prioritises_order_lookup_before_handoff_for_late_delivery():
    """Regression: an angry "still nothing after 2 weeks!!" message triggered
    an immediate handoff via the "customer is angry" rule, skipping the order
    lookup that could actually answer the question. A late/missing-order
    complaint should try lookup_order first and only hand off if that doesn't
    resolve it.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "hasn't arrived or is late" in prompt.lower()
    assert "try the order lookup first" in prompt.lower()


def test_is_shop_false_withholds_order_and_product_tools():
    """Regression: a "meta" assistant like tillbird-site (answers questions
    about Tillbird itself, not a shop's own support widget) still called
    lookup_order when asked about "order 1001" and returned the fashion-store
    demo fixture — confirmed live before this flag existed. A prompt
    instruction alone wasn't reliable enough; the tool must not be offered.
    """
    store = StoreConfig.model_validate({**BASE_STORE, "is_shop": False, "knowledge_base": {}})
    tool_names = [t["name"] for t in build_tool_schemas(store)]
    assert "search_products" not in tool_names
    assert "lookup_order" not in tool_names
    assert "handoff_to_human" in tool_names  # still useful even for a non-shop assistant


def test_is_shop_true_by_default_keeps_order_and_product_tools():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    tool_names = [t["name"] for t in build_tool_schemas(store)]
    assert "search_products" in tool_names
    assert "lookup_order" in tool_names


def test_is_shop_false_system_prompt_never_mentions_the_withheld_tools():
    store = StoreConfig.model_validate({**BASE_STORE, "is_shop": False, "knowledge_base": {}})
    prompt = build_system_prompt(store)
    assert "search_products" not in prompt
    assert "lookup_order" not in prompt
    assert "a UK Shopify store" not in prompt
    assert "never call, or claim to call, any order-lookup or product-search tool" in prompt.lower()


def test_is_shop_true_system_prompt_still_describes_the_store_as_a_shopify_store():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "a UK Shopify store" in prompt


def test_prompt_renders_overrides_as_the_top_priority_source():
    """B4: a manual correction (scripts/set_override.py) must appear in the
    prompt framed as the single most authoritative source, above the
    knowledge base, and must never be silently dropped."""
    store = StoreConfig.model_validate(
        {
            **BASE_STORE,
            "knowledge_base": {"shipping": "Dispatched within 2-3 working days.", "returns": "", "sizing": "", "faq": []},
            "overrides": [{"topic": "dispatch time", "answer": "We dispatch within 1-2 working days."}],
        }
    )
    prompt = build_system_prompt(store)
    assert "MANUAL CORRECTIONS" in prompt
    assert "dispatch time" in prompt.lower()
    assert "We dispatch within 1-2 working days." in prompt
    assert "most authoritative source" in prompt.lower()
    # Both the stale KB text and the override are present (the model sees
    # both, and the rule below tells it which one to trust) — the override
    # section itself must come before the knowledge base in the text.
    assert prompt.index("MANUAL CORRECTIONS") < prompt.index("Dispatched within 2-3 working days.")


def test_prompt_omits_overrides_section_when_none_set():
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "MANUAL CORRECTIONS" not in prompt


def test_prompt_instructs_preferring_the_higher_priority_source_on_disagreement():
    """B1: when sources disagree, the model must pick the more
    authoritative one (manual correction above all), never average or mix
    two numbers, and add a confirm-with-us caveat for a date/price/returns-
    window disagreement.
    """
    store = _store({"shipping": "", "returns": "", "sizing": "", "faq": []})
    prompt = build_system_prompt(store)
    assert "manual corrections above win over everything" in prompt.lower()
    assert "never average, blend, or split the difference" in prompt.lower()
    assert "1-3 days" in prompt.lower()  # the bad example is quoted so the model recognises what NOT to do
    assert "drop us a message and we'll confirm" in prompt.lower()
