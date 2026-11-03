import pytest
import yaml

from app.agent.loop import AgentResult, ToolCallRecord
from app.agent.order_lookup import LookupBudget, handle_lookup_order
from app.agent.tools import ToolContext
from app.models import ToolCall

STORE_DATA = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME",
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": [],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "live",
}


class _FakeShopifyClientAlwaysMisses:
    """live-mode Shopify stand-in: every lookup misses, like demo's fixed
    example order would for a guessed order number — these tests are about
    the session/IP budget mechanism, not demo-vs-live messaging (see
    test_order_lookup.py for the demo-explains-itself behaviour).
    """

    def __init__(self, shop_domain, api_version, access_token):
        pass

    def get_order(self, order_number, email):
        return None


@pytest.fixture
def store(app_config, monkeypatch):
    (app_config.stores_dir / "acme.yaml").write_text(yaml.safe_dump(STORE_DATA))
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test")
    monkeypatch.setattr("app.shopify_client.ShopifyAdminClient", _FakeShopifyClientAlwaysMisses)
    return "acme"


def _fake_agent_that_always_calls_lookup_order(order_number="9999", email="guess@example.com"):
    """Stands in for run_agent_turn: always makes exactly one lookup_order call,
    using the real handler and the real lookup_budget chat.py computed from
    SQLite — so this exercises the actual DB-backed enumeration guard end to
    end, without needing a real (or fake) Anthropic model in the loop.
    """

    def _run(app_config, store_config, history, message, client=None, lookup_budget=None):
        budget = lookup_budget or LookupBudget()
        ctx = ToolContext(store_config=store_config, app_config=app_config, lookup_budget=budget)
        result, status = handle_lookup_order(ctx, {"order_number": order_number, "email": email})
        handoff = status in ("blocked_session_limit", "blocked_ip_limit")
        return AgentResult(
            reply=result.get("message", "checked"),
            handoff=handoff,
            tool_calls=[ToolCallRecord(name="lookup_order", args={"order_number": order_number, "email": email}, status=status)],
        )

    return _run


def test_enumeration_across_session_ids_from_one_ip_is_blocked(client, app, store, monkeypatch):
    """A rotating session_id (e.g. clearing sessionStorage) must not let one IP
    bypass the per-session lookup cap — the per-IP cap (10/hour) should kick in
    across all of that IP's sessions.
    """
    monkeypatch.setattr("app.routes.chat.run_agent_turn", _fake_agent_that_always_calls_lookup_order())

    statuses = []
    for i in range(15):
        response = client.post(
            "/api/chat", json={"store_id": "acme", "session_id": f"rotating-session-{i}", "message": "check my order"}
        )
        assert response.status_code == 200
        with app.app_context():
            last_call = ToolCall.query.filter_by(name="lookup_order").order_by(ToolCall.id.desc()).first()
            statuses.append(last_call.status)

    # First 10 across all sessions go through (and miss, since order_number is a
    # guess); the rest are blocked by the shared per-IP budget regardless of
    # each request using a brand-new session_id.
    assert statuses.count("not_found") == 10
    assert statuses.count("blocked_ip_limit") == 5


def test_session_lookup_cap_blocks_after_five_attempts_in_one_session(client, app, store, monkeypatch):
    monkeypatch.setattr("app.routes.chat.run_agent_turn", _fake_agent_that_always_calls_lookup_order())

    statuses = []
    for _ in range(7):
        response = client.post(
            "/api/chat", json={"store_id": "acme", "session_id": "same-session", "message": "check my order"}
        )
        assert response.status_code == 200
        with app.app_context():
            last_call = (
                ToolCall.query.filter_by(name="lookup_order", session_id="same-session")
                .order_by(ToolCall.id.desc())
                .first()
            )
            statuses.append(last_call.status)

    assert statuses.count("not_found") == 5
    assert statuses.count("blocked_session_limit") == 2


def test_lookup_limit_reached_sets_handoff_true(client, app, store, monkeypatch):
    monkeypatch.setattr("app.routes.chat.run_agent_turn", _fake_agent_that_always_calls_lookup_order())

    last_response = None
    for _ in range(6):
        last_response = client.post(
            "/api/chat", json={"store_id": "acme", "session_id": "cap-session", "message": "check my order"}
        )
    assert last_response.get_json()["handoff"] is True
