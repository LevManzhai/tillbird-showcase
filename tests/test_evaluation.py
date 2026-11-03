from app.config import AppConfig
from app.evaluation import build_eval_cases, run_eval
from app.store_config import StoreConfig
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock

STORE_DATA = {
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
    "knowledge_base": {"shipping": "Free UK shipping.", "returns": "30 days.", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


def _store():
    return StoreConfig.model_validate(STORE_DATA)


def test_build_eval_cases_has_around_40_cases_covering_all_categories():
    cases = build_eval_cases()
    assert 35 <= len(cases) <= 45
    categories = {c.category for c in cases}
    assert categories == {
        "order_status",
        "returns",
        "sizing",
        "shipping_ireland",
        "angry_customer",
        "asks_for_discount",
        "prompt_injection",
        "asks_for_another_customers_order",
        "off_topic",
    }


def test_run_eval_flags_missing_lookup_order_call():
    cases = build_eval_cases()
    order_case = next(c for c in cases if c.category == "order_status")
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("I'm not sure, sorry.")])])

    results = run_eval(AppConfig(), _store(), cases=[order_case], client=client)
    assert results[0].flagged
    assert "lookup_order" in results[0].issues[0]


def test_run_eval_passes_when_lookup_order_called():
    cases = build_eval_cases()
    order_case = next(c for c in cases if c.category == "order_status")
    client = FakeAnthropicClient(
        [
            FakeResponse("tool_use", [FakeToolUseBlock("t1", "lookup_order", {"order_number": "1001", "email": "demo@example.com"})]),
            FakeResponse("end_turn", [FakeTextBlock("Your order is on its way!")]),
        ]
    )
    results = run_eval(AppConfig(), _store(), cases=[order_case], client=client)
    assert not results[0].flagged


def test_run_eval_flags_discount_promise():
    cases = build_eval_cases()
    discount_case = next(c for c in cases if c.category == "asks_for_discount")
    client = FakeAnthropicClient(
        [FakeResponse("end_turn", [FakeTextBlock("Sure! Here's a discount code: SAVE20 for 20% off your order.")])]
    )
    results = run_eval(AppConfig(), _store(), cases=[discount_case], client=client)
    assert results[0].flagged
    assert any("discount" in issue for issue in results[0].issues)


def test_run_eval_passes_when_discount_refused():
    cases = build_eval_cases()
    discount_case = next(c for c in cases if c.category == "asks_for_discount")
    client = FakeAnthropicClient(
        [FakeResponse("end_turn", [FakeTextBlock("I'm not able to offer discounts, sorry!")])]
    )
    results = run_eval(AppConfig(), _store(), cases=[discount_case], client=client)
    assert not results[0].flagged


def test_run_eval_flags_system_prompt_leak():
    cases = build_eval_cases()
    injection_case = next(c for c in cases if c.category == "prompt_injection")
    client = FakeAnthropicClient(
        [FakeResponse("end_turn", [FakeTextBlock("Sure! You are the AI customer support assistant for Acme...")])]
    )
    results = run_eval(AppConfig(), _store(), cases=[injection_case], client=client)
    assert results[0].flagged
    assert any("leak" in issue for issue in results[0].issues)


def test_run_eval_flags_off_topic_tool_call():
    cases = build_eval_cases()
    off_topic_case = next(c for c in cases if c.category == "off_topic")
    client = FakeAnthropicClient(
        [
            FakeResponse("tool_use", [FakeToolUseBlock("t1", "search_products", {"query": "paris"})]),
            FakeResponse("end_turn", [FakeTextBlock("Not sure that's related to us!")]),
        ]
    )
    results = run_eval(AppConfig(), _store(), cases=[off_topic_case], client=client)
    assert results[0].flagged


def test_run_eval_flags_angry_reply_with_no_handoff_language():
    cases = build_eval_cases()
    angry_case = next(c for c in cases if c.category == "angry_customer")
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Sorry to hear that.")])])
    results = run_eval(AppConfig(), _store(), cases=[angry_case], client=client)
    assert results[0].flagged


def test_run_eval_passes_angry_reply_that_offers_a_person():
    cases = build_eval_cases()
    angry_case = next(c for c in cases if c.category == "angry_customer")
    client = FakeAnthropicClient(
        [FakeResponse("end_turn", [FakeTextBlock("I'm really sorry — let me connect you with a member of our team.")])]
    )
    results = run_eval(AppConfig(), _store(), cases=[angry_case], client=client)
    assert not results[0].flagged
