from datetime import datetime, timedelta, timezone

import pytest
import yaml
from freezegun import freeze_time

from app.agent.loop import AgentResult
from app.extensions import db
from app.models import Message, Session
from app.routes.chat import DEMO_MESSAGE_CAP_DAILY, DEMO_MESSAGE_CAP_TOTAL

# Fixed instant, firmly mid-day and mid-month so no test in this file depends
# on the real wall-clock time — arithmetic like "1 hour ago" or "30 hours
# ago" must land on the intended side of a day boundary regardless of when
# the suite actually runs (see the test_demo_daily_cap flakiness this fixes:
# it failed for real whenever the suite ran within an hour of UTC midnight).
FROZEN_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

STORE_DATA = {
    "store_id": "demo-acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.example",
    "public_domain": "acme.example",
    "admin_token_env": None,
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": [],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


@pytest.fixture
def store(app_config):
    (app_config.stores_dir / "demo-acme.yaml").write_text(yaml.safe_dump(STORE_DATA))
    return "demo-acme"


def _fake_agent_turn():
    def _run(app_config, store_config, history, message, client=None, lookup_budget=None):
        _run.call_count += 1
        return AgentResult(reply="hi", handoff=False, tool_calls=[])

    _run.call_count = 0
    return _run


def _seed_messages(app, store_id, count, *, hours_ago=0.0, sessions=1):
    with app.app_context():
        created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        for i in range(sessions):
            db.session.add(Session(id=f"seed-session-{i}", store_id=store_id))
        db.session.flush()
        for i in range(count):
            session_id = f"seed-session-{i % sessions}"
            db.session.add(Message(session_id=session_id, role="user", content="hi", created_at=created_at))
        db.session.commit()


def test_demo_total_cap_blocks_across_different_sessions(client, app, store, monkeypatch):
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        # Spread across many sessions so the per-session (30) and per-IP/session
        # rate limits (20/10min) don't trip first — this is specifically testing
        # the store-wide total cap.
        _seed_messages(app, store, DEMO_MESSAGE_CAP_TOTAL, hours_ago=48, sessions=DEMO_MESSAGE_CAP_TOTAL)

        response = client.post(
            "/api/chat", json={"store_id": store, "session_id": "brand-new-session", "message": "hello"}
        )
    assert response.status_code == 200
    data = response.get_json()
    assert "usage limit" in data["reply"].lower()
    assert fake.call_count == 0


def test_demo_daily_cap_blocks_even_under_total_cap(client, app, store, monkeypatch):
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        _seed_messages(app, store, DEMO_MESSAGE_CAP_DAILY, hours_ago=1, sessions=DEMO_MESSAGE_CAP_DAILY)

        response = client.post(
            "/api/chat", json={"store_id": store, "session_id": "brand-new-session-2", "message": "hello"}
        )
    assert response.status_code == 200
    data = response.get_json()
    assert "usage limit" in data["reply"].lower()
    assert fake.call_count == 0


def test_demo_cap_resets_the_next_day(client, app, store, monkeypatch):
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        # Yesterday's messages count toward the total cap but not the daily one.
        _seed_messages(app, store, DEMO_MESSAGE_CAP_DAILY - 1, hours_ago=30, sessions=DEMO_MESSAGE_CAP_DAILY - 1)

        response = client.post(
            "/api/chat", json={"store_id": store, "session_id": "today-session", "message": "hello"}
        )
    assert response.status_code == 200
    assert fake.call_count == 1


def test_demo_daily_cap_override_triggers_below_the_global_default(client, app, app_config, monkeypatch):
    """stores/site-demo.yaml sets a much higher daily cap than the global
    default — but the override must work in both directions. Here a LOWER
    override (5) must trip before the global default (20) would.
    """
    store_id = "demo-lowcap"
    (app_config.stores_dir / f"{store_id}.yaml").write_text(
        yaml.safe_dump({**STORE_DATA, "store_id": store_id, "demo_daily_cap": 5})
    )
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        _seed_messages(app, store_id, 5, hours_ago=1, sessions=5)
        response = client.post(
            "/api/chat", json={"store_id": store_id, "session_id": "brand-new-session", "message": "hello"}
        )
    assert response.status_code == 200
    assert "usage limit" in response.get_json()["reply"].lower()
    assert fake.call_count == 0


def test_demo_total_cap_override_allows_more_than_the_global_default(client, app, app_config, monkeypatch):
    """A permanent flagship demo (site-demo) needs a much higher — or
    effectively disabled — lifetime total than a one-off sales demo.
    """
    store_id = "demo-highcap"
    (app_config.stores_dir / f"{store_id}.yaml").write_text(
        yaml.safe_dump({**STORE_DATA, "store_id": store_id, "demo_total_cap": 999999, "demo_daily_cap": 999999})
    )
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        # More than the global DEMO_MESSAGE_CAP_TOTAL (60), spread across many
        # sessions and days so nothing else trips first.
        _seed_messages(app, store_id, DEMO_MESSAGE_CAP_TOTAL + 10, hours_ago=48, sessions=DEMO_MESSAGE_CAP_TOTAL + 10)
        response = client.post(
            "/api/chat", json={"store_id": store_id, "session_id": "brand-new-session", "message": "hello"}
        )
    assert response.status_code == 200
    assert fake.call_count == 1


def test_demo_cap_message_override_is_used_when_set(client, app, app_config, monkeypatch):
    store_id = "demo-custommsg"
    custom_message = "Try one of the example questions above, or book a call!"
    (app_config.stores_dir / f"{store_id}.yaml").write_text(
        yaml.safe_dump({**STORE_DATA, "store_id": store_id, "demo_daily_cap": 1, "demo_cap_message": custom_message})
    )
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        _seed_messages(app, store_id, 1, hours_ago=1, sessions=1)
        response = client.post(
            "/api/chat", json={"store_id": store_id, "session_id": "brand-new-session", "message": "hello"}
        )
    assert response.get_json()["reply"] == custom_message


def test_max_session_messages_override_is_lower_than_global_default(client, app, app_config, monkeypatch):
    store_id = "demo-shortsession"
    (app_config.stores_dir / f"{store_id}.yaml").write_text(
        yaml.safe_dump({**STORE_DATA, "store_id": store_id, "max_session_messages": 2})
    )
    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    session_id = "one-session"
    with app.app_context():
        db.session.add(Session(id=session_id, store_id=store_id))
        db.session.add(Message(session_id=session_id, role="user", content="hi"))
        db.session.add(Message(session_id=session_id, role="user", content="hi again"))
        db.session.commit()

    response = client.post("/api/chat", json={"store_id": store_id, "session_id": session_id, "message": "hi again"})
    assert response.status_code == 200
    assert "reached its limit" in response.get_json()["reply"].lower()
    assert fake.call_count == 0


def test_live_mode_store_is_never_capped_by_demo_limits(client, app, monkeypatch):
    live_data = {
        **STORE_DATA,
        "store_id": "acme-live",
        "mode": "live",
        "shop_domain": "acme-live.myshopify.com",
        "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME_LIVE",
    }
    (client.application.extensions["app_config"].stores_dir / "acme-live.yaml").write_text(yaml.safe_dump(live_data))

    fake = _fake_agent_turn()
    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake)
    with freeze_time(FROZEN_NOW):
        _seed_messages(app, "acme-live", DEMO_MESSAGE_CAP_TOTAL + 10, hours_ago=1, sessions=DEMO_MESSAGE_CAP_TOTAL + 10)

        response = client.post(
            "/api/chat", json={"store_id": "acme-live", "session_id": "live-session", "message": "hi"}
        )
    assert response.status_code == 200
    assert fake.call_count == 1
