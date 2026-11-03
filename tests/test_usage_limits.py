from datetime import datetime, timedelta, timezone

from app.extensions import db
from app.models import SentNotification, Session
from app.store_config import StoreConfig
from app.usage_limits import (
    PLAN_LIMITS,
    check_and_alert_usage,
    conversations_this_month,
    is_over_hard_cap,
    monthly_limit_for,
)

STORE_DATA = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
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


def _store(**overrides):
    return StoreConfig.model_validate({**STORE_DATA, **overrides})


def _add_sessions(store_id, created_ats):
    for i, created_at in enumerate(created_ats):
        db.session.add(Session(id=f"{store_id}-{i}", store_id=store_id, created_at=created_at, last_active_at=created_at))
    db.session.commit()


# ---------- plan config ----------


def test_plan_limits_config():
    assert PLAN_LIMITS == {"small": 150, "starter": 500, "growth": 1500}


# ---------- conversations_this_month ----------


def test_conversations_this_month_counts_only_this_calendar_month(app):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with app.app_context():
        _add_sessions(
            "acme",
            [
                datetime(2026, 10, 1, tzinfo=timezone.utc),
                datetime(2026, 10, 20, tzinfo=timezone.utc),
                datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc),  # last month, excluded
                datetime(2026, 11, 1, tzinfo=timezone.utc),  # next month, excluded
            ],
        )
        assert conversations_this_month("acme", now) == 2


def test_conversations_this_month_excludes_other_stores(app):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with app.app_context():
        _add_sessions("acme", [datetime(2026, 10, 1, tzinfo=timezone.utc)])
        _add_sessions("bravo", [datetime(2026, 10, 1, tzinfo=timezone.utc)] * 3)
        assert conversations_this_month("acme", now) == 1


def test_conversations_this_month_handles_december_rollover(app):
    now = datetime(2026, 12, 15, tzinfo=timezone.utc)
    with app.app_context():
        _add_sessions(
            "acme",
            [datetime(2026, 12, 1, tzinfo=timezone.utc), datetime(2027, 1, 1, tzinfo=timezone.utc)],
        )
        assert conversations_this_month("acme", now) == 1


# ---------- monthly_limit_for ----------


def test_monthly_limit_for_none_when_unset():
    assert monthly_limit_for(_store()) is None


def test_monthly_limit_for_explicit_override():
    assert monthly_limit_for(_store(plan="starter", monthly_conversation_limit=1500)) == 1500


def test_monthly_limit_for_plan_default():
    assert monthly_limit_for(_store(plan="growth")) == 1500


def test_monthly_limit_for_small_plan_default():
    assert monthly_limit_for(_store(plan="small")) == 150


# ---------- 80%/100%/150% thresholds computed from the small plan's 300 ----------


def test_small_plan_80_percent_threshold_alert(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(plan="small"), count=240, limit=300, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert len(sent) == 1
    assert "80%" in sent[0]["subject"]


def test_small_plan_100_percent_threshold_alert(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(plan="small"), count=300, limit=300, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert len(sent) == 2  # both 80% and 100% have now been crossed
    assert any("100%" in s["subject"] for s in sent)


def test_small_plan_150_percent_hard_cap():
    assert is_over_hard_cap(count=450, limit=300) is False  # exactly 150% — still answering
    assert is_over_hard_cap(count=451, limit=300) is True  # just over — hard capped


# ---------- is_over_hard_cap ----------


def test_is_over_hard_cap_at_exactly_150_percent_is_not_over():
    assert is_over_hard_cap(count=1500, limit=1000) is False


def test_is_over_hard_cap_above_150_percent():
    assert is_over_hard_cap(count=1501, limit=1000) is True


def test_is_over_hard_cap_well_under():
    assert is_over_hard_cap(count=500, limit=1000) is False


# ---------- check_and_alert_usage ----------


def test_check_and_alert_usage_sends_at_80_percent(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert len(sent) == 1
    assert "80%" in sent[0]["subject"]


def test_check_and_alert_usage_sends_both_thresholds_once_each(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=1100, limit=1000, now=now)
    assert len(sent) == 2
    assert {("80%" in s["subject"]) for s in sent} | {("100%" in s["subject"]) for s in sent}


def test_check_and_alert_usage_does_not_resend_the_same_threshold_same_month(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=now)
        check_and_alert_usage(app_config, _store(), count=850, limit=1000, now=now)  # still just 80%, same month
    assert len(sent) == 1


def test_check_and_alert_usage_resends_in_a_new_month(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=datetime(2026, 11, 15, tzinfo=timezone.utc))
    assert len(sent) == 2


def test_check_and_alert_usage_below_threshold_sends_nothing(app, app_config, monkeypatch):
    app_config.alert_email = "lev@tillbird.co.uk"
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=100, limit=1000, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert sent == []


def test_check_and_alert_usage_noop_without_alert_email(app, app_config, monkeypatch):
    app_config.alert_email = ""
    sent = []
    monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=1200, limit=1000, now=datetime(2026, 10, 15, tzinfo=timezone.utc))
    assert sent == []


def test_check_and_alert_usage_never_raises_on_send_failure_and_does_not_mark_sent(app, app_config, monkeypatch):
    """A failed send must be retried on the next check, not silently
    recorded as sent — otherwise an SMTP blip during the one request that
    happened to cross a threshold would mean Lev never finds out at all.
    """
    app_config.alert_email = "lev@tillbird.co.uk"

    def _raise(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.usage_limits.send_alert_email", _raise)
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    with app.app_context():
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=now)  # must not raise
        assert db.session.query(SentNotification).count() == 0

        sent = []
        monkeypatch.setattr("app.usage_limits.send_alert_email", lambda *a, **k: sent.append(k))
        check_and_alert_usage(app_config, _store(), count=800, limit=1000, now=now)
        assert len(sent) == 1  # retried successfully this time
