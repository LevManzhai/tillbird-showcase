"""Shared Anthropic cost-tracking wrapper — used by any script that makes
real, billed Claude calls and wants to report what it actually spent
(scripts/batch_demos.py, scripts/audit_store.py). Moved here from
batch_demos.py so both scripts (and any future one) share one definition
instead of drifting.
"""

# Anthropic list pricing, USD per million tokens. Source: anthropic.com/pricing
# as of 2026 — update this (or ANTHROPIC_MODEL) if either changes; this is an
# estimate, not an invoice. cache_write/cache_read are prompt-caching rates
# (see app.agent.loop's cache_control usage) — standard ratios to the base
# input price (1.25x to write a 5-minute cache entry, 0.1x to read one) are
# applied here rather than looked up separately per model, since Anthropic's
# cache pricing has historically moved with the base input price. Used by
# scripts/usage_report.py; not verified against a live invoice.
PRICING_PER_MODEL = {
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_write": 0.125, "cache_read": 0.01},
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1},
}
DEFAULT_PRICING = {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2}


class UsageTrackingClient:
    """Wraps a real anthropic.Anthropic client to tally input/output tokens
    across every call made through it. Duck-types the
    `client.messages.create(...)` shape the rest of the codebase expects,
    so it can be passed anywhere a real client is accepted.

    Tracks both a global running total (total_input_tokens/
    total_output_tokens/call_count — unchanged from before, used by
    scripts/batch_demos.py which only ever calls one model) AND a
    per-model breakdown (usage_by_model), needed once a single run can mix
    models (scripts/audit_store.py: a cheap model for extraction/
    classification, the main model only for the final contradiction
    check) — pricing one model's tokens at another model's rate would
    misreport cost, so cost_so_far(model) (price the global total at one
    model's rate) is only correct for a single-model run; use
    total_cost()/cost_for_model() for anything that mixes models.
    """

    def __init__(self, real_client):
        self._real = real_client
        self.messages = self
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.call_count = 0
        self.usage_by_model: dict[str, dict[str, int]] = {}

    def create(self, **kwargs):
        response = self._real.messages.create(**kwargs)
        self.total_input_tokens += response.usage.input_tokens
        self.total_output_tokens += response.usage.output_tokens
        self.call_count += 1
        model = kwargs.get("model", "")
        bucket = self.usage_by_model.setdefault(model, {"input": 0, "output": 0, "calls": 0})
        bucket["input"] += response.usage.input_tokens
        bucket["output"] += response.usage.output_tokens
        bucket["calls"] += 1
        return response

    def cost_so_far(self, model: str) -> float:
        pricing = PRICING_PER_MODEL.get(model, DEFAULT_PRICING)
        return (self.total_input_tokens / 1_000_000) * pricing["input"] + (
            self.total_output_tokens / 1_000_000
        ) * pricing["output"]

    def cost_for_model(self, model: str) -> float:
        bucket = self.usage_by_model.get(model, {"input": 0, "output": 0})
        pricing = PRICING_PER_MODEL.get(model, DEFAULT_PRICING)
        return (bucket["input"] / 1_000_000) * pricing["input"] + (bucket["output"] / 1_000_000) * pricing["output"]

    def total_cost(self) -> float:
        return sum(self.cost_for_model(m) for m in self.usage_by_model)
