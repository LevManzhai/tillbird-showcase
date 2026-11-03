from app.config import AppConfig
from app.email_sender import build_handoff_message, build_lead_message, is_smtp_configured, send_handoff_email, send_lead_email


def _app_config(**overrides):
    config = AppConfig()
    config.smtp_host = overrides.get("smtp_host", "smtp.example.com")
    config.smtp_port = overrides.get("smtp_port", 587)
    config.smtp_username = overrides.get("smtp_username", "user@example.com")
    config.smtp_password = overrides.get("smtp_password", "secret")
    config.smtp_from = overrides.get("smtp_from", "support-bot@example.com")
    return config


def test_is_smtp_configured_true_when_host_and_from_set():
    assert is_smtp_configured(_app_config()) is True


def test_is_smtp_configured_false_when_host_missing():
    assert is_smtp_configured(_app_config(smtp_host="")) is False


def test_is_smtp_configured_false_when_from_missing():
    assert is_smtp_configured(_app_config(smtp_from="")) is False


def test_build_handoff_message_includes_required_fields():
    transcript = [
        {"role": "user", "content": "Where is my order?"},
        {"role": "assistant", "content": "Let me check that for you."},
    ]
    msg = build_handoff_message(
        store_name="Acme Fashion",
        support_email="support@acme.co.uk",
        from_address="bot@acme.co.uk",
        customer_email="jane@example.com",
        summary="Order arrived damaged",
        transcript=transcript,
    )
    assert msg["To"] == "support@acme.co.uk"
    assert msg["From"] == "bot@acme.co.uk"
    assert msg["Reply-To"] == "jane@example.com"
    assert msg["Subject"] == "Customer question needs you: Order arrived damaged"

    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in msg.walk() if part.get_content_maintype() != "multipart"}
    plain = parts["text/plain"]
    html = parts["text/html"]

    for body in (plain, html):
        assert "jane@example.com" in body
        assert "Order arrived damaged" in body
        assert "Where is my order?" in body
        assert "Let me check that for you." in body
        assert "Acme Fashion" in body


def test_build_handoff_message_escapes_html_but_not_plain_text():
    transcript = [{"role": "user", "content": "<script>alert(1)</script>"}]
    msg = build_handoff_message(
        store_name="Acme",
        support_email="support@acme.co.uk",
        from_address="bot@acme.co.uk",
        customer_email="jane@example.com",
        summary="",
        transcript=transcript,
    )
    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in msg.walk() if part.get_content_maintype() != "multipart"}
    assert "<script>alert(1)</script>" not in parts["text/html"]
    assert "&lt;script&gt;" in parts["text/html"]
    assert "<script>alert(1)</script>" in parts["text/plain"]


def test_build_handoff_message_empty_transcript_and_summary():
    msg = build_handoff_message(
        store_name="Acme",
        support_email="support@acme.co.uk",
        from_address="bot@acme.co.uk",
        customer_email="jane@example.com",
        summary="",
        transcript=[],
    )
    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in msg.walk() if part.get_content_maintype() != "multipart"}
    assert "(no messages)" in parts["text/plain"]
    assert "(none provided)" in parts["text/plain"]
    assert msg["Subject"] == "Customer question needs you: a customer question"


def test_build_handoff_message_truncates_a_long_summary_in_the_subject_at_a_word_boundary():
    long_summary = "The customer says their order arrived completely smashed and they want a full refund immediately please"
    msg = build_handoff_message(
        store_name="Acme",
        support_email="support@acme.co.uk",
        from_address="bot@acme.co.uk",
        customer_email="jane@example.com",
        summary=long_summary,
        transcript=[],
    )
    subject = msg["Subject"]
    assert subject.startswith("Customer question needs you: ")
    assert subject.endswith("…")
    assert len(subject) < len(f"Customer question needs you: {long_summary}")
    assert not subject[:-1].endswith(" ")  # trimmed at a word boundary, not mid-word


def test_build_handoff_message_reply_to_is_the_customer_email():
    msg = build_handoff_message(
        store_name="Acme",
        support_email="support@acme.co.uk",
        from_address="bot@acme.co.uk",
        customer_email="jane@example.com",
        summary="Where's my order?",
        transcript=[],
    )
    assert msg["Reply-To"] == "jane@example.com"


class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.started_tls = False
        self.login_args = None
        self.sent_message = None
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.started_tls = True

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, msg):
        self.sent_message = msg

    def quit(self):
        self.quit_called = True


def test_send_handoff_email_uses_starttls_for_non_ssl_port(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    config = _app_config(smtp_port=587)

    send_handoff_email(
        config,
        store_name="Acme",
        support_email="support@acme.co.uk",
        customer_email="jane@example.com",
        summary="issue",
        transcript=[],
    )

    assert len(_FakeSMTP.instances) == 1
    instance = _FakeSMTP.instances[0]
    assert instance.started_tls is True
    assert instance.login_args == ("user@example.com", "secret")
    assert instance.sent_message is not None
    assert instance.quit_called is True


def test_send_handoff_email_uses_ssl_for_port_465(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSMTP)
    config = _app_config(smtp_port=465)

    send_handoff_email(
        config,
        store_name="Acme",
        support_email="support@acme.co.uk",
        customer_email="jane@example.com",
        summary="issue",
        transcript=[],
    )

    assert len(_FakeSMTP.instances) == 1
    assert _FakeSMTP.instances[0].started_tls is False  # SSL already encrypted, no separate STARTTLS
    assert _FakeSMTP.instances[0].sent_message is not None


def test_send_handoff_email_skips_login_when_no_username(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    config = _app_config(smtp_username="")

    send_handoff_email(
        config, store_name="Acme", support_email="s@acme.co.uk", customer_email="j@example.com", summary="", transcript=[]
    )
    assert _FakeSMTP.instances[0].login_args is None


def test_send_handoff_email_raises_on_smtp_failure(monkeypatch):
    class _FailingSMTP(_FakeSMTP):
        def send_message(self, msg):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", _FailingSMTP)
    config = _app_config()

    import pytest

    with pytest.raises(OSError):
        send_handoff_email(
            config, store_name="Acme", support_email="s@acme.co.uk", customer_email="j@example.com", summary="", transcript=[]
        )


def test_build_lead_message_includes_required_fields():
    msg = build_lead_message(
        to_address="lev@tillbird.co.uk",
        from_address="bot@tillbird.co.uk",
        store_url="https://example-shop.co.uk",
        email="jane@example.com",
        name="Jane",
    )
    assert msg["To"] == "lev@tillbird.co.uk"
    assert msg["From"] == "bot@tillbird.co.uk"
    assert "example-shop.co.uk" in msg["Subject"]

    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in msg.walk() if part.get_content_maintype() != "multipart"}
    for body in (parts["text/plain"], parts["text/html"]):
        assert "https://example-shop.co.uk" in body
        assert "jane@example.com" in body
        assert "Jane" in body


def test_build_lead_message_handles_missing_name():
    msg = build_lead_message(
        to_address="lev@tillbird.co.uk",
        from_address="bot@tillbird.co.uk",
        store_url="https://example-shop.co.uk",
        email="jane@example.com",
        name="",
    )
    parts = {part.get_content_type(): part.get_payload(decode=True).decode("utf-8") for part in msg.walk() if part.get_content_maintype() != "multipart"}
    assert "(not given)" in parts["text/plain"]


def test_send_lead_email_sends_via_smtp(monkeypatch):
    _FakeSMTP.instances = []
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    config = _app_config(smtp_port=587)

    send_lead_email(config, to_address="lev@tillbird.co.uk", store_url="https://example-shop.co.uk", email="jane@example.com", name="Jane")

    assert len(_FakeSMTP.instances) == 1
    assert _FakeSMTP.instances[0].sent_message["To"] == "lev@tillbird.co.uk"
