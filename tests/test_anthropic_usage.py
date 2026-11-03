import pytest

from app.anthropic_usage import UsageTrackingClient
from tests.fake_anthropic import FakeResponse, FakeTextBlock, FakeUsage


class _FakeRealMessages:
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
        return self._responses.pop(0)


class _FakeRealClient:
    def __init__(self, responses):
        self.messages = _FakeRealMessages(responses)


def test_usage_tracking_client_tallies_tokens_across_calls():
    real = _FakeRealClient(
        [
            FakeResponse("end_turn", [FakeTextBlock("a")], usage=FakeUsage(1000, 200)),
            FakeResponse("end_turn", [FakeTextBlock("b")], usage=FakeUsage(500, 100)),
        ]
    )
    client = UsageTrackingClient(real)
    client.messages.create(model="x")
    client.messages.create(model="x")

    assert client.total_input_tokens == 1500
    assert client.total_output_tokens == 300
    assert client.call_count == 2


def test_usage_tracking_client_computes_cost_for_known_model():
    real = _FakeRealClient([FakeResponse("end_turn", [FakeTextBlock("a")], usage=FakeUsage(1_000_000, 1_000_000))])
    client = UsageTrackingClient(real)
    client.messages.create(model="x")
    cost = client.cost_so_far("claude-sonnet-5")
    assert cost == pytest.approx(2.0 + 10.0)


def test_usage_tracking_client_falls_back_to_default_pricing_for_unknown_model():
    real = _FakeRealClient([FakeResponse("end_turn", [FakeTextBlock("a")], usage=FakeUsage(1_000_000, 0))])
    client = UsageTrackingClient(real)
    client.messages.create(model="some-future-model")
    cost = client.cost_so_far("some-future-model")
    assert cost == pytest.approx(2.0)


def test_usage_tracking_client_total_cost_prices_each_model_at_its_own_rate():
    """Mixed-model usage (a cheap model for most calls, the main model for
    just one) must not have its cheap-model tokens priced at the main
    model's rate, or vice versa — total_cost() sums each model's own
    bucket at its own price, unlike cost_so_far(model) which only prices
    the global total correctly for a single-model run.
    """
    real = _FakeRealClient(
        [
            FakeResponse("end_turn", [FakeTextBlock("a")], usage=FakeUsage(1_000_000, 1_000_000)),
            FakeResponse("end_turn", [FakeTextBlock("b")], usage=FakeUsage(1_000_000, 1_000_000)),
        ]
    )
    client = UsageTrackingClient(real)
    client.messages.create(model="claude-haiku-4-5-20251001")
    client.messages.create(model="claude-sonnet-5")

    assert client.cost_for_model("claude-haiku-4-5-20251001") == pytest.approx(1.0 + 5.0)
    assert client.cost_for_model("claude-sonnet-5") == pytest.approx(2.0 + 10.0)
    assert client.total_cost() == pytest.approx(1.0 + 5.0 + 2.0 + 10.0)
