import pytest
import yaml

from app import create_app
from app.agent.loop import AgentResult
from app.config import AppConfig

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
    "allowed_origins": [],  # deliberately empty — only the own-origin exemption should let requests through
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


@pytest.fixture
def proxied_app_config(tmp_path):
    config = AppConfig()
    config.database_uri = "sqlite:///:memory:"
    config.stores_dir = tmp_path / "stores"
    config.stores_dir.mkdir()
    config.trusted_proxy_count = 1
    (config.stores_dir / "acme.yaml").write_text(yaml.safe_dump(STORE_DATA))
    return config


@pytest.fixture
def proxied_client(proxied_app_config):
    flask_app = create_app(proxied_app_config)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def test_forwarded_headers_are_trusted_when_proxy_count_configured(proxied_client):
    response = proxied_client.get(
        "/health",
        headers={"X-Forwarded-Proto": "https", "X-Forwarded-Host": "emberhome.uk"},
        base_url="http://internal-container:8000",
    )
    assert response.status_code == 200


def test_own_origin_exemption_matches_forwarded_https_host(proxied_client, monkeypatch):
    def fake_run_agent_turn(app_config, store_config, history, message, client=None, lookup_budget=None):
        return AgentResult(reply="hi", handoff=False, tool_calls=[])

    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake_run_agent_turn)

    # The real client connection looks like it's hitting an internal
    # container address, but Caddy (simulated here) forwards the true
    # public scheme/host — the own-origin exemption must match against
    # *that*, not the raw internal connection, or a real https demo page
    # would get rejected by its own server.
    response = proxied_client.post(
        "/api/chat",
        json={"store_id": "acme", "session_id": "s1", "message": "hi"},
        headers={
            "Origin": "https://emberhome.uk",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "emberhome.uk",
        },
        base_url="http://internal-container:8000",
    )
    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == "https://emberhome.uk"


def test_without_forwarded_headers_internal_origin_still_own_origin(proxied_client, monkeypatch):
    """Even behind a proxy, a request with no forwarded headers at all should
    still work using whatever host Werkzeug actually sees — ProxyFix only
    overrides when the headers are present.
    """

    def fake_run_agent_turn(app_config, store_config, history, message, client=None, lookup_budget=None):
        return AgentResult(reply="hi", handoff=False, tool_calls=[])

    monkeypatch.setattr("app.routes.chat.run_agent_turn", fake_run_agent_turn)

    response = proxied_client.post(
        "/api/chat",
        json={"store_id": "acme", "session_id": "s2", "message": "hi"},
        headers={"Origin": "http://localhost"},
    )
    assert response.status_code == 200
