"""Model-compatibility helpers for Claude calls that need one specific tool
call back (structured extraction via a single tool's input).

Claude Sonnet 5.5 / Opus 5.5 / Fable 5.1 reject a forced tool_choice
(`{"type": "tool", ...}` or `{"type": "any"}`) with a 400. On those models
the call goes out with tool_choice "auto" plus an explicit instruction to
call the tool, extra max_tokens headroom (adaptive thinking runs before an
unforced tool call and counts toward max_tokens), and one retry if the
model answered in text instead. Older models keep the forced call exactly
as before.
"""

_NO_FORCED_TOOL_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-mythos-5-1")

# Thinking tokens count toward max_tokens once the call is unforced — without
# this headroom a call sized for the tool input alone can hit max_tokens
# mid-thinking and never reach the tool call.
THINKING_HEADROOM_TOKENS = 8000


def supports_forced_tool_choice(model: str) -> bool:
    return not any(model == m or model.startswith(m + "-") or model.endswith("." + m) for m in _NO_FORCED_TOOL_MODELS)


def _has_tool_call(response, tool_name: str) -> bool:
    return any(
        getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name
        for block in response.content
    )


def create_with_tool(client, *, model: str, tool_name: str, messages: list, max_tokens: int, **kwargs):
    """client.messages.create(...) that asks for exactly one call to
    `tool_name`. Returns the raw response; callers keep their own
    tool_use parsing and missing-call error handling."""
    if supports_forced_tool_choice(model):
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
            **kwargs,
        )

    instruction = f"\n\nRespond only by calling the `{tool_name}` tool — do not reply in plain text."
    *earlier, last = messages
    if isinstance(last.get("content"), str):
        messages = [*earlier, {**last, "content": last["content"] + instruction}]

    response = None
    for _ in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens + THINKING_HEADROOM_TOKENS,
            tool_choice={"type": "auto"},
            messages=messages,
            **kwargs,
        )
        if _has_tool_call(response, tool_name) or response.stop_reason in ("max_tokens", "refusal"):
            break
    return response
