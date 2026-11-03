import yaml

from app.extensions import db
from app.models import Lead

STORE_DATA = {
    "store_id": "site-demo",
    "store_name": "Hearth & Holt",
    "shop_domain": "hearth-and-holt.myshopify.com",
    "public_domain": "hearthandholt.co.uk",
    "admin_token_env": None,
    "support_email": "hello@hearthandholt.co.uk",
    "tone": "Warm",
    "brand_color": "#2f4d3a",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": ["https://tillbird.co.uk"],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


def _write_store(app_config):
    (app_config.stores_dir / "site-demo.yaml").write_text(yaml.safe_dump(STORE_DATA))


def _valid_payload(**overrides):
    return {"store_url": "example-shop.co.uk", "email": "jane@example.com", "name": "Jane", **overrides}


def test_lead_stores_valid_submission(client, app):
    response = client.post("/api/lead", json=_valid_payload())
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    with app.app_context():
        leads = Lead.query.all()
        assert len(leads) == 1
        assert leads[0].store_url == "https://example-shop.co.uk"
        assert leads[0].email == "jane@example.com"
        assert leads[0].name == "Jane"


def test_lead_missing_fields_rejected(client):
    response = client.post("/api/lead", json={"store_url": "", "email": ""})
    assert response.status_code == 400


def test_lead_invalid_email_rejected(client):
    response = client.post("/api/lead", json=_valid_payload(email="not-an-email"))
    assert response.status_code == 400


def test_lead_invalid_store_url_rejected(client):
    response = client.post("/api/lead", json=_valid_payload(store_url="not a url at all with spaces"))
    assert response.status_code == 400


def test_lead_name_is_optional(client, app):
    response = client.post("/api/lead", json={"store_url": "example-shop.co.uk", "email": "jane@example.com"})
    assert response.status_code == 200
    with app.app_context():
        assert Lead.query.first().name is None


def test_lead_honeypot_silently_succeeds_without_storing(client, app):
    response = client.post("/api/lead", json=_valid_payload(company="I am a bot"))
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    with app.app_context():
        assert Lead.query.count() == 0


def test_lead_rate_limited_after_max_per_ip(client, app):
    for _ in range(5):
        response = client.post("/api/lead", json=_valid_payload())
        assert response.status_code == 200
    response = client.post("/api/lead", json=_valid_payload())
    assert response.status_code == 429
    with app.app_context():
        assert Lead.query.count() == 5


def test_lead_stored_even_when_smtp_not_configured(client, app):
    response = client.post("/api/lead", json=_valid_payload())
    assert response.status_code == 200
    with app.app_context():
        lead = Lead.query.first()
        assert lead is not None
        assert lead.email_sent is False


def test_lead_sends_email_when_smtp_configured(client, app, app_config, monkeypatch):
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@tillbird.co.uk"

    sent = {}

    def _fake_send(config, *, to_address, store_url, email, name):
        sent["to_address"] = to_address
        sent["store_url"] = store_url
        sent["email"] = email

    monkeypatch.setattr("app.routes.leads.send_lead_email", _fake_send)
    response = client.post("/api/lead", json=_valid_payload())
    assert response.status_code == 200
    assert sent["email"] == "jane@example.com"
    with app.app_context():
        assert Lead.query.first().email_sent is True


def test_lead_email_failure_does_not_break_the_response(client, app, app_config, monkeypatch):
    app_config.smtp_host = "smtp.example.com"
    app_config.smtp_from = "bot@tillbird.co.uk"

    def _fake_send(*args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("app.routes.leads.send_lead_email", _fake_send)
    response = client.post("/api/lead", json=_valid_payload())
    assert response.status_code == 200
    with app.app_context():
        assert Lead.query.first().email_sent is False


def test_lead_cors_allowed_origin_from_a_store_allowlist(client, app_config):
    _write_store(app_config)
    response = client.post("/api/lead", json=_valid_payload(), headers={"Origin": "https://tillbird.co.uk"})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "https://tillbird.co.uk"


def test_lead_cors_rejects_unknown_origin(client, app_config):
    _write_store(app_config)
    response = client.post("/api/lead", json=_valid_payload(), headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403


def test_lead_preflight_reflects_allowed_origin(client, app_config):
    _write_store(app_config)
    response = client.options("/api/lead", headers={"Origin": "https://tillbird.co.uk"})
    assert response.headers.get("Access-Control-Allow-Origin") == "https://tillbird.co.uk"
