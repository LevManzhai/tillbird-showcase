import pytest
import yaml

from app.agent.handoff import MISSING_EMAIL_MESSAGE, confirmation_message, handle_handoff_to_human
from app.agent.tools import ToolContext
from app.config import AppConfig
from app.models import Handoff, Message
from app.store_config import StoreConfig

STORE_DATA = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "admin_token_env": None,
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": ["https://acme.co.uk"],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


DEFAULT_CONFIRMATION = confirmation_message(StoreConfig.model_validate(STORE_DATA))


@pytest.fixture
def store(app_config):
    (app_config.stores_dir / "acme.yaml").write_text(yaml.safe_dump(STORE_DATA))
    return "acme"


def test_handle_handoff_to_human_succeeds(caplog):
    store = StoreConfig.model_validate(STORE_DATA)
    ctx = ToolContext(store_config=store, app_config=AppConfig())
    result, status = handle_handoff_to_human(ctx, {"customer_email": "jane@example.com", "summary": "damaged item"})
    assert status == "ok"
    assert result["handed_off"] is True
    assert result["message"] == confirmation_message(store)


def test_confirmation_message_never_promises_a_response_time_by_default():
    store = StoreConfig.model_validate(STORE_DATA)
    message = confirmation_message(store)
    assert message == "Thanks — I've passed this on to our team. They'll get back to you by email."


def test_confirmation_message_includes_the_configured_response_time():
    store = StoreConfig.model_validate({**STORE_DATA, "handoff_response_time": "We usually reply within one business day."})
    message = confirmation_message(store)
    assert "We usually reply within one business day." in message


def test_handle_handoff_to_human_missing_email():
    store = StoreConfig.model_validate(STORE_DATA)
    ctx = ToolContext(store_config=store, app_config=AppConfig())
    result, status = handle_handoff_to_human(ctx, {"customer_email": "", "summary": "help"})
    assert status == "error"
    assert result["handed_off"] is False
    assert result["message"] == MISSING_EMAIL_MESSAGE


def test_api_handoff_endpoint_creates_handoff_row(client, app, store):
    response = client.post(
        "/api/handoff",
        json={
            "store_id": "acme",
            "session_id": "sess-1",
            "customer_email": "jane@example.com",
            "summary": "wrong item received",
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["handoff"] is True
    assert data["reply"] == DEFAULT_CONFIRMATION

    with app.app_context():
        row = Handoff.query.filter_by(session_id="sess-1").first()
        assert row is not None
        assert row.customer_email == "jane@example.com"
        assert row.summary == "wrong item received"
        assert row.email_sent is False

        messages = Message.query.filter_by(session_id="sess-1").order_by(Message.id).all()
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].content == DEFAULT_CONFIRMATION


def test_api_handoff_missing_email_400(client, store):
    response = client.post(
        "/api/handoff", json={"store_id": "acme", "session_id": "sess-1", "customer_email": "", "summary": "x"}
    )
    assert response.status_code == 400


def test_api_handoff_unknown_store_404(client):
    response = client.post(
        "/api/handoff",
        json={"store_id": "nope", "session_id": "sess-1", "customer_email": "a@b.com", "summary": "x"},
    )
    assert response.status_code == 404


def test_api_handoff_own_origin_allowed_even_when_not_in_allowed_origins(client, store):
    response = client.post(
        "/api/handoff",
        json={"store_id": "acme", "session_id": "sess-own", "customer_email": "a@b.com", "summary": "x"},
        headers={"Origin": "http://localhost"},
        base_url="http://localhost",
    )
    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == "http://localhost"


def test_api_handoff_third_party_origin_rejected(client, store):
    response = client.post(
        "/api/handoff",
        json={"store_id": "acme", "session_id": "sess-2", "customer_email": "a@b.com", "summary": "x"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403


class _FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=None):
        pass

    def starttls(self):
        pass

    def login(self, username, password):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)

    def quit(self):
        pass


def test_api_handoff_sends_real_email_when_smtp_configured(client, app, app_config, store, monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@acme.co.uk"

    response = client.post(
        "/api/handoff",
        json={"store_id": "acme", "session_id": "smtp-sess", "customer_email": "jane@example.com", "summary": "issue"},
    )
    assert response.status_code == 200
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["To"] == "support@acme.co.uk"
    assert _FakeSMTP.sent[0]["Reply-To"] == "jane@example.com"
    assert _FakeSMTP.sent[0]["Subject"] == "Customer question needs you: issue"

    with app.app_context():
        row = Handoff.query.filter_by(session_id="smtp-sess").first()
        assert row.email_sent is True


def test_api_handoff_routes_to_handoff_email_when_set(client, app, app_config, monkeypatch):
    """handoff_email overrides where the email goes — defaults to
    support_email when unset (see the test above)."""
    _FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@acme.co.uk"
    (app_config.stores_dir / "acme.yaml").write_text(
        yaml.safe_dump({**STORE_DATA, "handoff_email": "handoffs@acme.co.uk"})
    )

    client.post(
        "/api/handoff",
        json={"store_id": "acme", "session_id": "handoff-email-sess", "customer_email": "jane@example.com", "summary": "issue"},
    )
    assert len(_FakeSMTP.sent) == 1
    assert _FakeSMTP.sent[0]["To"] == "handoffs@acme.co.uk"


def test_api_handoff_email_includes_full_transcript(client, app, app_config, store, monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@acme.co.uk"

    with app.app_context():
        from app.extensions import db
        from app.models import Session as SessionModel

        db.session.add(SessionModel(id="transcript-sess", store_id="acme"))
        db.session.add(Message(session_id="transcript-sess", role="user", content="Where is my order 1015?"))
        db.session.add(Message(session_id="transcript-sess", role="assistant", content="Let me check that."))
        db.session.commit()

    client.post(
        "/api/handoff",
        json={
            "store_id": "acme",
            "session_id": "transcript-sess",
            "customer_email": "jane@example.com",
            "summary": "wrong item",
        },
    )
    assert len(_FakeSMTP.sent) == 1
    body_parts = [
        part.get_payload(decode=True).decode("utf-8")
        for part in _FakeSMTP.sent[0].walk()
        if part.get_content_maintype() == "text"
    ]
    combined = "\n".join(body_parts)
    assert "Where is my order 1015?" in combined
    assert "Let me check that." in combined
    assert "[Talk to a person] wrong item" in combined  # the handoff's own note is part of the transcript too


def test_api_handoff_falls_back_to_log_only_on_smtp_failure(client, app, app_config, store, monkeypatch):
    class _FailingSMTP(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", _FailingSMTP)
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@acme.co.uk"

    response = client.post(
        "/api/handoff",
        json={"store_id": "acme", "session_id": "smtp-fail-sess", "customer_email": "jane@example.com", "summary": "x"},
    )
    # Customer must never see an error for this, even if the real send fails.
    assert response.status_code == 200
    assert response.get_json()["reply"] == DEFAULT_CONFIRMATION

    with app.app_context():
        row = Handoff.query.filter_by(session_id="smtp-fail-sess").first()
        assert row.email_sent is False
