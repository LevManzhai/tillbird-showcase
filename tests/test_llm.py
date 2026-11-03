from app.llm import THINKING_HEADROOM_TOKENS, create_with_tool, supports_forced_tool_choice
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse, FakeTextBlock, FakeToolUseBlock


def _tool_response(name="pick"):
    return FakeResponse("tool_use", [FakeToolUseBlock("t1", name, {"x": 1})])


def test_supports_forced_tool_choice_by_model():
    assert supports_forced_tool_choice("claude-sonnet-5")
    assert supports_forced_tool_choice("claude-haiku-4-5-20251001")
    assert not supports_forced_tool_choice("claude-sonnet-5-5")
    assert not supports_forced_tool_choice("claude-opus-5-5")
    assert not supports_forced_tool_choice("anthropic.claude-sonnet-5-5")


def test_older_model_keeps_forced_tool_choice_unchanged():
    client = FakeAnthropicClient([_tool_response()])
    messages = [{"role": "user", "content": "hi"}]
    create_with_tool(client, model="claude-sonnet-5", tool_name="pick", messages=messages, max_tokens=500, tools=[])

    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "pick"}
    assert call["max_tokens"] == 500
    assert call["messages"] == messages


def test_sonnet_5_5_uses_auto_with_instruction_and_headroom():
    client = FakeAnthropicClient([_tool_response()])
    create_with_tool(
        client, model="claude-sonnet-5-5", tool_name="pick", messages=[{"role": "user", "content": "hi"}], max_tokens=500
    )

    call = client.messages.calls[0]
    assert call["tool_choice"] == {"type": "auto"}
    assert call["max_tokens"] == 500 + THINKING_HEADROOM_TOKENS
    assert call["messages"][-1]["content"].startswith("hi")
    assert "`pick`" in call["messages"][-1]["content"]


def test_sonnet_5_5_retries_once_when_model_replies_in_text():
    client = FakeAnthropicClient([FakeResponse("end_turn", [FakeTextBlock("sure")]), _tool_response()])
    response = create_with_tool(
        client, model="claude-sonnet-5-5", tool_name="pick", messages=[{"role": "user", "content": "hi"}], max_tokens=10
    )

    assert len(client.messages.calls) == 2
    assert response.content[0].name == "pick"


def test_sonnet_5_5_does_not_retry_a_truncated_response():
    client = FakeAnthropicClient([FakeResponse("max_tokens", [FakeTextBlock("cut")])])
    response = create_with_tool(
        client, model="claude-sonnet-5-5", tool_name="pick", messages=[{"role": "user", "content": "hi"}], max_tokens=10
    )

    assert len(client.messages.calls) == 1
    assert response.stop_reason == "max_tokens"
