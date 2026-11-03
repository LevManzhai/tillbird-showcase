from datetime import datetime, timezone

from app.extensions import db
from app.models import Handoff, Message, Session
from app.reporting import generate_monthly_report, month_bounds, render_markdown
from tests.fake_anthropic import FakeAnthropicClient, FakeResponse


class _FakeToolUseBlock:
    def __init__(self, id, name, input):
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


def test_month_bounds():
    start, end = month_bounds("2026-09")
    assert start == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert end.day == 30
    assert end.month == 9


def _seed(app, store_id, in_month, out_of_month):
    with app.app_context():
        for i, dt in enumerate(in_month):
            sid = f"in-{i}"
            db.session.add(Session(id=sid, store_id=store_id, created_at=dt))
            db.session.add(Message(session_id=sid, role="user", content=f"question {i}", created_at=dt))
            db.session.add(Message(session_id=sid, role="assistant", content="answer", created_at=dt))
        for i, dt in enumerate(out_of_month):
            sid = f"out-{i}"
            db.session.add(Session(id=sid, store_id=store_id, created_at=dt))
            db.session.add(Message(session_id=sid, role="user", content="should not count", created_at=dt))
        db.session.commit()


def test_generate_monthly_report_counts_only_in_month_sessions(app, app_config):
    september = datetime(2026, 9, 15, tzinfo=timezone.utc)
    august = datetime(2026, 8, 15, tzinfo=timezone.utc)
    _seed(app, "acme", in_month=[september, september], out_of_month=[august])

    fake_client = FakeAnthropicClient(
        [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "categorize_topics", {"topics": [{"topic": "General", "count": 2}]})])]
    )

    with app.app_context():
        report = generate_monthly_report(db.session, app_config, "acme", "2026-09", client=fake_client)

    assert report.total_conversations == 2
    assert report.total_messages == 4  # 2 sessions x (user+assistant)
    assert report.resolved_without_handoff_pct == 100.0
    assert report.handoff_count == 0
    assert len(report.example_conversations) == 2


def test_generate_monthly_report_computes_handoff_percentage(app, app_config):
    dt = datetime(2026, 9, 10, tzinfo=timezone.utc)
    with app.app_context():
        for i in range(4):
            sid = f"sess-{i}"
            db.session.add(Session(id=sid, store_id="acme", created_at=dt))
            db.session.add(Message(session_id=sid, role="user", content="hi", created_at=dt))
        db.session.add(Handoff(session_id="sess-0", customer_email="a@b.com", summary="x", email_sent=False))
        db.session.commit()

        fake_client = FakeAnthropicClient(
            [FakeResponse("tool_use", [_FakeToolUseBlock("t1", "categorize_topics", {"topics": []})])]
        )
        report = generate_monthly_report(db.session, app_config, "acme", "2026-09", client=fake_client)

    assert report.total_conversations == 4
    assert report.handoff_count == 1
    assert report.resolved_without_handoff_pct == 75.0


def test_generate_monthly_report_empty_month_returns_zeros(app, app_config):
    with app.app_context():
        report = generate_monthly_report(db.session, app_config, "acme", "2026-09", client=FakeAnthropicClient([]))

    assert report.total_conversations == 0
    assert report.total_messages == 0
    assert report.resolved_without_handoff_pct == 0.0
    assert report.topics == []
    assert report.example_conversations == []


def test_generate_monthly_report_only_includes_matching_store(app, app_config):
    dt = datetime(2026, 9, 10, tzinfo=timezone.utc)
    with app.app_context():
        db.session.add(Session(id="acme-sess", store_id="acme", created_at=dt))
        db.session.add(Session(id="other-sess", store_id="other-store", created_at=dt))
        db.session.commit()

        report = generate_monthly_report(db.session, app_config, "acme", "2026-09", client=FakeAnthropicClient([]))

    assert report.total_conversations == 1


def test_render_markdown_includes_key_sections():
    from app.reporting import MonthlyReport

    report = MonthlyReport(
        store_id="acme",
        month="2026-09",
        total_conversations=5,
        total_messages=20,
        resolved_without_handoff_pct=80.0,
        handoff_count=1,
        order_lookup_count=3,
        topics=[{"topic": "Shipping", "count": 3}],
        example_conversations=[[{"role": "user", "content": "hi", "created_at": "2026-09-01T00:00:00+00:00"}]],
    )
    md = render_markdown(report)
    assert "# acme — 2026-09" in md
    assert "80.0%" in md
    assert "Shipping: 3" in md
    assert "Conversation 1" in md
    assert "hi" in md
