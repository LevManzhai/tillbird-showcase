class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeToolUseBlock:
    def __init__(self, id, name, input):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


class FakeUsage:
    def __init__(self, input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class FakeResponse:
    def __init__(self, stop_reason, content, usage=None):
        self.stop_reason = stop_reason
        self.content = content
        self.usage = usage or FakeUsage()


class _FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAnthropicClient:
    """Duck-typed stand-in for anthropic.Anthropic, queueing canned responses."""

    def __init__(self, responses):
        self.messages = _FakeMessagesAPI(responses)
        self.with_options_calls = []

    def with_options(self, **kwargs):
        # The real SDK returns a new client; this fake just records what it
        # was asked for and returns itself, since `.messages` (and its
        # `.calls` list) needs to stay the one every test already asserts
        # against — each with_options() call is immediately followed by
        # exactly one .messages.create() in app.agent.loop, so the Nth
        # entry here lines up with the Nth entry in self.messages.calls.
        self.with_options_calls.append(kwargs)
        return self
