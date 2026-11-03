import json
import logging
import time
from dataclasses import dataclass, field

import anthropic

from app.agent.order_lookup import LookupBudget
from app.agent.system_prompt import build_system_prompt
from app.agent.tools import ToolContext, build_tool_schemas, call_tool

_logger = logging.getLogger("app.agent.loop")

MAX_TOOL_ITERATIONS = 4
# Headroom for tool-call JSON (e.g. several search_products calls in one
# turn) plus a full reply — 400 was tight enough that a token-heavy tool
# turn could get cut off (stop_reason "max_tokens") before any reply text
# was written at all. Replies are kept short via the system prompt's own
# "2-4 sentences" instruction, not by starving the token budget.
# Adaptive thinking counts toward max_tokens (claude-sonnet-5-5 thinks before
# replying from "medium" effort up), so this leaves room for the thinking
# plus a short customer-facing reply — only tokens actually generated are billed.
MAX_REPLY_TOKENS = 4096
FALLBACK_REPLY = "Sorry, I'm having trouble with that — let me get a person to help you."

# gunicorn kills the whole worker (SystemExit from handle_abort), not just
# the slow call, if a /api/chat request runs past its own --timeout (60s —
# see docker-entrypoint.sh) — confirmed live, a single messages.create()
# blocked on a slow SSL read did exactly that, and the customer got a dead
# connection instead of any reply at all. ANTHROPIC_REQUEST_TIMEOUT_SECONDS
# + ANTHROPIC_MAX_RETRIES bounds any ONE call; TURN_DEADLINE_SECONDS bounds
# the whole turn (which can chain up to MAX_TOOL_ITERATIONS round-trips)
# well inside gunicorn's limit, leaving headroom for the DB write/commit
# that follows in app/routes/chat.py.
ANTHROPIC_REQUEST_TIMEOUT_SECONDS = 20.0
ANTHROPIC_MAX_RETRIES = 1
TURN_DEADLINE_SECONDS = 40.0

# Confirmed live: the same refund/returns question asked repeatedly against
# the same store got inconsistent answers — some runs correctly recited a
# returns exclusion ("personal care goods ... can't be returned") and then,
# in the same reply, still told the customer to go ahead and return the
# excluded item. This installed SDK's API has no `temperature` parameter at
# all (checked: Messages.create() has no such kwarg) — the available lever
# for "think more carefully/consistently" in this API generation is
# `output_config.effort` instead. Only applied when the CUSTOMER's message
# looks like a refund/return/exchange/delivery/warranty question, not every
# turn — "high" effort costs more latency, and this turn budget is already
# tight (see TURN_DEADLINE_SECONDS/gunicorn's own 60s worker timeout above).
POLICY_EFFORT = "high"
# Everything else (greetings, product/stock questions, order lookups) runs at
# "low": on claude-sonnet-5-5 the effort levels are recalibrated and the API
# default is "high", which thinks before almost every reply — slower first
# token and more output tokens for no gain on simple chat.
CHAT_EFFORT = "low"
_POLICY_QUESTION_KEYWORDS = {
    "refund", "refunds", "return", "returns", "exchange", "exchanges",
    "shipping", "delivery", "deliver", "dispatch", "postage", "ship", "post",
    "warranty", "guarantee", "faulty", "broken", "defective", "damaged",
}


def _is_policy_question(message: str) -> bool:
    lowered = message.lower()
    return any(keyword in lowered for keyword in _POLICY_QUESTION_KEYWORDS)


def _call_budget(remaining: float) -> tuple[float, int]:
    """(timeout, max_retries) for the NEXT messages.create() call, given
    how much of the turn deadline is left. The call itself is never given
    more than `remaining` to run (min(ANTHROPIC_REQUEST_TIMEOUT_SECONDS,
    remaining)) — a late call starting with e.g. 5s left on a 40s turn
    deadline must not still be allowed its own full 20s, or the turn as a
    whole could blow past the deadline it was supposed to respect. The
    configured retry is only allowed when there's enough left for a full
    second attempt at the full per-call timeout — otherwise a timed-out
    first attempt plus a retry could together run long after the turn
    deadline (and, in the worst case, past gunicorn's own --timeout) even
    though each individual attempt stayed under ANTHROPIC_REQUEST_TIMEOUT_SECONDS.
    """
    timeout = max(0.0, min(ANTHROPIC_REQUEST_TIMEOUT_SECONDS, remaining))
    max_retries = ANTHROPIC_MAX_RETRIES if remaining >= 2 * ANTHROPIC_REQUEST_TIMEOUT_SECONDS else 0
    return timeout, max_retries


def _timeout_fallback_reply(store_config) -> str:
    """Friendly, store-voiced fallback for a turn_timeout or Anthropic API
    error — always points at the store's own real contact address (never
    an invented one), same as the session/demo-cap fallbacks in
    app/routes/chat.py.
    """
    return f"Sorry, I'm taking too long on that one. You can email us at {store_config.support_email} and we'll get back to you."


@dataclass
class ToolCallRecord:
    name: str
    args: dict
    status: str


@dataclass
class TurnUsage:
    """Anthropic usage for one whole turn, summed across however many
    messages.create() calls it took (a tool-use turn can chain several
    round-trips — see MAX_TOOL_ITERATIONS). Logged onto the assistant
    Message row this turn produces (see app.routes.chat, app.models
    .Message) — cache_creation/cache_read reflect the system-prompt+tools
    cache_control breakpoint below (see run_agent_turn).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage) -> None:
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_creation_input_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_input_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0


@dataclass
class AgentResult:
    reply: str
    handoff: bool
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    usage: TurnUsage = field(default_factory=TurnUsage)


def _extract_text(response) -> str:
    parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    return "\n".join(parts).strip()


def run_agent_turn(
    app_config,
    store_config,
    history: list[dict],
    user_message: str,
    client=None,
    lookup_budget: LookupBudget | None = None,
) -> AgentResult:
    client = client or anthropic.Anthropic(
        api_key=app_config.anthropic_api_key,
        timeout=ANTHROPIC_REQUEST_TIMEOUT_SECONDS,
        max_retries=ANTHROPIC_MAX_RETRIES,
    )
    system_prompt = build_system_prompt(store_config)
    # One cache_control breakpoint, on the system prompt — tool definitions
    # are serialised before `system` in the request, so this one breakpoint
    # caches tools + system prompt + the store's knowledge base/overrides
    # (already inlined into system_prompt by build_system_prompt) together
    # as a single stable prefix; only `messages` (the actual conversation)
    # varies turn to turn. Verified against the installed anthropic==1.9.0
    # SDK's own type stubs (TextBlockParam/ToolParam both take
    # cache_control: CacheControlEphemeralParam — see anthropic.types), not
    # guessed; the minimum prompt length the API will actually cache below
    # is documented prose, not something in the SDK types, so it's NOT
    # verified here — if cache_read_input_tokens/cache_creation_input_tokens
    # stay at 0 in the usage log below after this ships, check
    # platform.claude.com/docs for the current minimum against this store's
    # actual system-prompt length.
    cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    messages = [*history, {"role": "user", "content": user_message}]
    # Computed once per turn from the customer's own message — every
    # messages.create() call in this turn (including any tool round-trips
    # chained off the same question) gets the same effort setting.
    effort = POLICY_EFFORT if _is_policy_question(user_message) else CHAT_EFFORT
    extra_create_kwargs = {"output_config": {"effort": effort}}
    ctx = ToolContext(store_config=store_config, app_config=app_config, lookup_budget=lookup_budget or LookupBudget())
    tool_schemas = build_tool_schemas(store_config)
    tool_calls: list[ToolCallRecord] = []
    usage = TurnUsage()
    handoff_signal = False
    turn_deadline = time.monotonic() + TURN_DEADLINE_SECONDS

    for _ in range(MAX_TOOL_ITERATIONS):
        now = time.monotonic()
        if now >= turn_deadline:
            _logger.warning("turn_timeout store_id=%s", store_config.store_id)
            return AgentResult(reply=_timeout_fallback_reply(store_config), handoff=True, tool_calls=tool_calls, usage=usage)

        call_timeout, call_retries = _call_budget(turn_deadline - now)
        try:
            response = client.with_options(max_retries=call_retries).messages.create(
                model=app_config.anthropic_model,
                max_tokens=MAX_REPLY_TOKENS,
                system=cached_system,
                tools=tool_schemas,
                messages=messages,
                timeout=call_timeout,
                **extra_create_kwargs,
            )
        except anthropic.APIError as exc:
            # Covers a slow/timed-out call, a rate limit, and an overloaded/
            # 5xx response alike — the customer must never see a raw error
            # or a request that just hangs, only this same friendly fallback.
            _logger.warning("anthropic_api_error store_id=%s error_type=%s", store_config.store_id, type(exc).__name__)
            return AgentResult(reply=_timeout_fallback_reply(store_config), handoff=True, tool_calls=tool_calls, usage=usage)

        usage.add(response.usage)

        if response.stop_reason != "tool_use":
            text = _extract_text(response)
            if not text:
                # e.g. stop_reason "max_tokens" cut the response off before any
                # customer-facing text was written — never send a blank reply.
                return AgentResult(reply=FALLBACK_REPLY, handoff=True, tool_calls=tool_calls, usage=usage)
            return AgentResult(reply=text, handoff=handoff_signal, tool_calls=tool_calls, usage=usage)

        messages.append({"role": "assistant", "content": response.content})
        tool_result_blocks = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            result, status = call_tool(block.name, block.input, ctx)
            tool_calls.append(ToolCallRecord(name=block.name, args=block.input, status=status))
            if status in ("blocked_session_limit", "blocked_ip_limit"):
                handoff_signal = True
            if block.name == "handoff_to_human" and status == "ok":
                handoff_signal = True
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})

    return AgentResult(reply=FALLBACK_REPLY, handoff=True, tool_calls=tool_calls, usage=usage)
