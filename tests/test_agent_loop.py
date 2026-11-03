import anthropic
import httpx

import app.agent.loop as loop_module
from app.agent.loop import (
    ANTHROPIC_MAX_RETRIES,
    ANTHROPIC_REQUEST_TIMEOUT_SECONDS,
    MAX_REPLY_TOKENS,
    MAX_TOOL_ITERATIONS,
    FALLBACK_REPLY,
    POLICY_EFFORT,
    TURN_DEADLINE_SECONDS,
    _call_budget,
    _is_policy_question,
    run_agent_turn,
)
from app.config import AppConfig
from app.store_config import StoreConfig
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock, FakeUsage

STORE_DATA = {
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
    "knowledge_base": {"shipping": "Royal Mail, 2-3 days.", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


def _store():
    return StoreConfig.model_validate(STORE_DATA)


def _app_config(tmp_path):
    config = AppConfig()
    config.stores_dir = tmp_path
    return config


def test_run_agent_turn_no_tool_use(tmp_path):
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Hello there!")])])
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert result.reply == "Hello there!"
    assert result.handoff is False
    assert result.tool_calls == []


def test_run_agent_turn_requests_enough_tokens_for_tool_calls_plus_a_reply(tmp_path):
    """Regression: max_tokens=400 was tight enough that a turn with several
    tool calls could get cut off before any reply text was written at all
    (see test_run_agent_turn_falls_back_when_response_has_no_text). Replies
    are kept short via the system prompt's own instruction, not by starving
    this budget, so it should comfortably exceed a single short reply.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Hello there!")])])
    run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert client.messages.calls[0]["max_tokens"] == MAX_REPLY_TOKENS
    assert MAX_REPLY_TOKENS >= 1024


def test_is_policy_question_matches_refund_return_delivery_warranty_keywords():
    for message in [
        "Can I get a refund?",
        "What's your returns policy?",
        "Can I exchange this for a different size?",
        "How long does delivery take?",
        "Is there a warranty on this?",
        "It arrived damaged, what do I do?",
    ]:
        assert _is_policy_question(message), f"expected {message!r} to be classified as a policy question"


def test_is_policy_question_false_for_unrelated_messages():
    assert _is_policy_question("What's the capital of France?") is False
    assert _is_policy_question("Do you have this in blue?") is False


def test_run_agent_turn_uses_high_effort_for_a_policy_question(tmp_path):
    """Regression: the same refund/returns question asked repeatedly got
    inconsistent answers — some runs correctly recited a returns exclusion
    and then, in the same reply, still told the customer to go ahead and
    return the excluded item. This installed SDK has no `temperature`
    parameter at all; `output_config.effort` is the available lever.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Sure, happy to help.")])])
    run_agent_turn(_app_config(tmp_path), _store(), [], "Can I get a refund?", client=client)
    assert client.messages.calls[0]["output_config"] == {"effort": POLICY_EFFORT}


def test_run_agent_turn_uses_low_effort_for_a_non_policy_question(tmp_path):
    """High effort costs real latency (the turn budget is already tight —
    see TURN_DEADLINE_SECONDS) — must not be applied to every single turn,
    only ones that actually look like a refund/return/delivery/warranty
    question. Everything else is set to "low" explicitly rather than left to
    the API default, which is "high" on claude-sonnet-5-5.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Sure, happy to help.")])])
    run_agent_turn(_app_config(tmp_path), _store(), [], "Do you have this in blue?", client=client)
    assert client.messages.calls[0]["output_config"] == {"effort": "low"}


def test_run_agent_turn_calls_tool_then_answers(tmp_path):
    client = FakeAnthropicClient(
        [
            FakeResponse("tool_use", [FakeToolUseBlock("t1", "get_policy", {"topic": "shipping"})]),
            FakeResponse("end_turn", [FakeTextBlock("We ship via Royal Mail, 2-3 days.")]),
        ]
    )
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "how does shipping work?", client=client)
    assert result.reply == "We ship via Royal Mail, 2-3 days."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "get_policy"
    assert result.tool_calls[0].status == "ok"
    assert len(client.messages.calls) == 2


def test_run_agent_turn_exceeds_iterations_falls_back_to_handoff(tmp_path):
    responses = [
        FakeResponse("tool_use", [FakeToolUseBlock(f"t{i}", "get_policy", {"topic": "x"})])
        for i in range(MAX_TOOL_ITERATIONS)
    ]
    client = FakeAnthropicClient(responses)
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "??", client=client)
    assert result.reply == FALLBACK_REPLY
    assert result.handoff is True
    assert len(result.tool_calls) == MAX_TOOL_ITERATIONS


def test_run_agent_turn_falls_back_when_response_has_no_text(tmp_path):
    """Regression: a response that stops for a reason other than "tool_use"
    (e.g. "max_tokens" cutting generation off before any text was written)
    but carries no text block used to be returned verbatim as an empty
    string — a blank reply sent straight to the customer. It must fall back
    to a safe message and signal a handoff instead.
    """
    client = FakeAnthropicClient([FakeResponse("max_tokens", [FakeToolUseBlock("t1", "search_products", {})])])
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "??", client=client)
    assert result.reply == FALLBACK_REPLY
    assert result.handoff is True


def test_run_agent_turn_unknown_tool_is_logged_as_error(tmp_path):
    client = FakeAnthropicClient(
        [
            FakeResponse("tool_use", [FakeToolUseBlock("t1", "not_a_real_tool", {})]),
            FakeResponse("end_turn", [FakeTextBlock("Sorry, let me check.")]),
        ]
    )
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "??", client=client)
    assert result.tool_calls[0].status == "error"


# ---------- A3: gunicorn-timeout resilience ----------


def test_run_agent_turn_falls_back_on_anthropic_timeout(tmp_path):
    """A slow/timed-out Anthropic call must never hang the request or
    surface a raw error — the customer gets a friendly, store-voiced
    fallback pointing at the store's real contact email, and a handoff."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    client = FakeAnthropicClient([anthropic.APITimeoutError(request=request)])
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "how long is delivery?", client=client)
    assert "support@acme.co.uk" in result.reply
    assert "Sorry, I'm taking too long" in result.reply
    assert result.handoff is True


def test_run_agent_turn_falls_back_on_anthropic_rate_limit(tmp_path):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    client = FakeAnthropicClient([anthropic.RateLimitError("rate limited", response=response, body=None)])
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert "support@acme.co.uk" in result.reply
    assert result.handoff is True


def test_run_agent_turn_stops_looping_once_the_turn_deadline_is_passed(tmp_path, monkeypatch):
    """Simulates a turn that's already run long across earlier tool-use
    round-trips (e.g. two slow-but-not-timed-out calls) — the NEXT
    iteration must not even attempt another call once TURN_DEADLINE_SECONDS
    has elapsed, so a chatty tool-use loop can never itself blow past
    gunicorn's --timeout the way the single-slow-call case did.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("should never be reached")])])
    clock = iter([0.0, TURN_DEADLINE_SECONDS + 1])  # first call sets the deadline, second check is already past it
    monkeypatch.setattr(loop_module.time, "monotonic", lambda: next(clock))
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert "support@acme.co.uk" in result.reply
    assert result.handoff is True
    assert client.messages.calls == []  # never even attempted the call


def test_call_budget_shrinks_timeout_and_drops_the_retry_near_the_deadline():
    """A call starting with only 5s left on the turn deadline must not
    still get its own full 20s + a retry — both could individually stay
    under ANTHROPIC_REQUEST_TIMEOUT_SECONDS yet together blow past the
    turn (and gunicorn's own) timeout. It gets whatever's actually left,
    and no retry (there isn't room for a second full attempt).
    """
    timeout, max_retries = _call_budget(5.0)
    assert timeout == 5.0
    assert max_retries == 0


def test_call_budget_gives_the_full_budget_when_plenty_of_deadline_remains():
    timeout, max_retries = _call_budget(TURN_DEADLINE_SECONDS)
    assert timeout == ANTHROPIC_REQUEST_TIMEOUT_SECONDS
    assert max_retries == ANTHROPIC_MAX_RETRIES


def test_run_agent_turn_shrinks_the_anthropic_call_budget_near_the_deadline(tmp_path, monkeypatch):
    """End-to-end version of test_call_budget_shrinks_timeout_and_drops_the_retry_near_the_deadline:
    a call that starts with only 5s left on the 40s turn deadline must be
    made with timeout=5 and max_retries=0, not the full 20s/1 retry.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("hi")])])
    clock = iter([0.0, 35.0])  # turn_deadline = 40.0; this call starts with 5s left
    monkeypatch.setattr(loop_module.time, "monotonic", lambda: next(clock))
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert result.reply == "hi"
    assert client.messages.calls[-1]["timeout"] == 5.0
    assert client.with_options_calls[-1]["max_retries"] == 0


def test_run_agent_turn_constructs_the_real_client_with_a_bounded_timeout_and_retries(tmp_path, monkeypatch):
    """When no client is injected (the real production path), the real
    anthropic.Anthropic client must be built with the tight per-call
    timeout + single retry that bounds any one call well inside
    gunicorn's own --timeout (see docker-entrypoint.sh)."""
    captured = {}

    class _CapturingClient(FakeAnthropicClient):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__([FakeResponse("end_turn", [FakeTextBlock("hi")])])

    monkeypatch.setattr(loop_module.anthropic, "Anthropic", _CapturingClient)
    run_agent_turn(_app_config(tmp_path), _store(), [], "hi")
    assert captured["timeout"] == ANTHROPIC_REQUEST_TIMEOUT_SECONDS
    assert captured["max_retries"] == ANTHROPIC_MAX_RETRIES


# ---------- C1: prompt caching + usage logging ----------


def test_run_agent_turn_marks_the_system_prompt_as_cacheable(tmp_path):
    """One cache_control breakpoint on the system prompt — tools are
    serialised before `system`, so this one breakpoint is meant to cache
    tool definitions + system prompt + the store's knowledge base/
    overrides (already inlined by build_system_prompt) together as a
    single stable prefix, since only `messages` varies turn to turn.
    """
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("Hello there!")])])
    run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    system_param = client.messages.calls[0]["system"]
    assert system_param == [
        {"type": "text", "text": system_param[0]["text"], "cache_control": {"type": "ephemeral"}}
    ]
    assert "Royal Mail" in system_param[0]["text"]  # the real system prompt text, not swapped for a placeholder


def test_run_agent_turn_returns_usage_for_a_single_call_turn(tmp_path):
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "end_turn",
                [FakeTextBlock("Hello there!")],
                usage=FakeUsage(input_tokens=500, output_tokens=40, cache_creation_input_tokens=300, cache_read_input_tokens=0),
            )
        ]
    )
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 40
    assert result.usage.cache_creation_input_tokens == 300
    assert result.usage.cache_read_input_tokens == 0


def test_run_agent_turn_sums_usage_across_a_tool_use_round_trip(tmp_path):
    """A turn that calls a tool makes TWO messages.create() calls — usage
    must be the SUM of both, not just the last one, since both are real
    billed calls for this one turn.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [FakeToolUseBlock("t1", "get_policy", {"topic": "shipping"})],
                usage=FakeUsage(input_tokens=500, output_tokens=20, cache_read_input_tokens=400),
            ),
            FakeResponse(
                "end_turn",
                [FakeTextBlock("We ship via Royal Mail.")],
                usage=FakeUsage(input_tokens=550, output_tokens=30, cache_read_input_tokens=400),
            ),
        ]
    )
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "how does shipping work?", client=client)
    assert result.usage.input_tokens == 1050
    assert result.usage.output_tokens == 50
    assert result.usage.cache_read_input_tokens == 800


def test_run_agent_turn_reports_usage_so_far_even_on_a_timeout_fallback(tmp_path, monkeypatch):
    """A turn that times out on its SECOND call must still report the
    first call's real, billed usage — that cost happened and must be
    logged, even though the turn itself ended in a fallback reply.
    """
    client = FakeAnthropicClient(
        [
            FakeResponse(
                "tool_use",
                [FakeToolUseBlock("t1", "get_policy", {"topic": "shipping"})],
                usage=FakeUsage(input_tokens=500, output_tokens=20),
            ),
            FakeResponse("end_turn", [FakeTextBlock("should never be reached")]),
        ]
    )
    clock = iter([0.0, 10.0, TURN_DEADLINE_SECONDS + 1])
    monkeypatch.setattr(loop_module.time, "monotonic", lambda: next(clock))
    result = run_agent_turn(_app_config(tmp_path), _store(), [], "hi", client=client)
    assert result.usage.input_tokens == 500
    assert result.usage.output_tokens == 20
    assert "support@acme.co.uk" in result.reply
