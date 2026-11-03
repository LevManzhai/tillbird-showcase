import yaml

STORE_DATA = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "admin_token_env": None,
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#123456",
    "greeting": "Hi there!",
    "hint_text": "Need a hand?",
    "allowed_origins": ["https://acme.co.uk"],
    "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
    "privacy_policy_url": "https://acme.co.uk/privacy",
}


def _write_store(app_config, data=None):
    (app_config.stores_dir / "acme.yaml").write_text(yaml.safe_dump(data or STORE_DATA))


def test_root_redirects_to_marketing_site(client):
    # "/" 404ing (this host has no landing page) was getting hit by
    # scanners; redirect somewhere useful instead of a bare 404.
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "https://tillbird.co.uk"


def test_widget_js_served_with_cors_and_js_mimetype(client):
    response = client.get("/widget.js")
    assert response.status_code == 200
    assert "javascript" in response.content_type
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert "data-store" in response.get_data(as_text=True)


def test_widget_js_under_40kb(client):
    # Not a hard technical limit — a soft budget so the embed script stays a
    # trivial payload on a customer's storefront (it's also cached 5 minutes,
    # see Cache-Control below). Raised from 30KB as legitimate features
    # (inline mode, chips, autoplay, conversation persistence) and the
    # comments documenting a couple of genuinely subtle bugs they required
    # fixing (flexbox/shadow-DOM height chains) have grown it — trimming
    # those comments to hit an arbitrary number isn't worth it.
    response = client.get("/widget.js")
    assert len(response.get_data()) < 40 * 1024


def test_widget_config_returns_public_fields_only(client, app_config):
    _write_store(app_config)
    response = client.get("/api/widget-config/acme")
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    data = response.get_json()
    assert data == {
        "store_name": "Acme Fashion",
        "brand_color": "#123456",
        "greeting": "Hi there!",
        "hint_text": "Need a hand?",
        "privacy_policy_url": "https://acme.co.uk/privacy",
        "widget_tagline": None,
        "tillbird_privacy_url": "https://tillbird.co.uk/privacy",
        "disabled": False,
    }
    # never leak internal/security-relevant fields
    assert "allowed_origins" not in data
    assert "forbidden" not in data
    assert "admin_token_env" not in data
    assert "support_email" not in data


def test_widget_config_tillbird_privacy_url_follows_app_config(client, app_config):
    _write_store(app_config)
    app_config.tillbird_privacy_url = "https://tillbird.co.uk/custom-privacy"
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["tillbird_privacy_url"] == "https://tillbird.co.uk/custom-privacy"


def test_widget_config_includes_tagline_when_set(client, app_config):
    _write_store(app_config, {**STORE_DATA, "widget_tagline": "This assistant runs on Tillbird too."})
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["widget_tagline"] == "This assistant runs on Tillbird too."


def test_widget_config_disabled_for_paused_live_store(client, app_config):
    _write_store(app_config, {**STORE_DATA, "mode": "live", "shop_domain": "acme.myshopify.com", "status": "paused"})
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["disabled"] is True


def test_widget_config_disabled_for_expired_trial_live_store(client, app_config):
    _write_store(
        app_config,
        {**STORE_DATA, "mode": "live", "shop_domain": "acme.myshopify.com", "status": "trial", "trial_ends_at": "2020-01-01"},
    )
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["disabled"] is True


def test_widget_config_not_disabled_for_active_in_progress_trial(client, app_config):
    _write_store(
        app_config,
        {**STORE_DATA, "mode": "live", "shop_domain": "acme.myshopify.com", "status": "trial", "trial_ends_at": "2999-01-01"},
    )
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["disabled"] is False


def test_widget_config_not_disabled_for_demo_store_regardless_of_status(client, app_config):
    """Demo stores keep their own separate expiry logic — status/
    trial_ends_at must never affect them, even if set by mistake."""
    _write_store(app_config, {**STORE_DATA, "status": "cancelled"})
    response = client.get("/api/widget-config/acme")
    assert response.get_json()["disabled"] is False


def test_widget_config_unknown_store_404(client):
    response = client.get("/api/widget-config/nope")
    assert response.status_code == 404


def test_demo_page_renders_store_name_and_script_tag(client, app_config):
    _write_store(app_config)
    response = client.get("/demo/acme")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Acme Fashion" in html
    assert 'data-store="acme"' in html
    assert 'src="/widget.js"' in html


def test_demo_page_unknown_store_404(client):
    response = client.get("/demo/nope")
    assert response.status_code == 404


def test_demo_page_sends_noindex_header_and_meta(client, app_config):
    _write_store(app_config)
    response = client.get("/demo/acme")
    assert response.headers.get("X-Robots-Tag") == "noindex, nofollow"
    assert 'name="robots" content="noindex, nofollow"' in response.get_data(as_text=True)


def test_demo_page_shows_unofficial_notice_for_demo_mode(client, app_config):
    _write_store(app_config)  # mode: demo
    html = client.get("/demo/acme").get_data(as_text=True)
    assert "Unofficial demo built by Tillbird" in html


def test_demo_page_omits_unofficial_notice_for_live_mode(client, app_config):
    live_data = {**STORE_DATA, "mode": "live", "shop_domain": "acme.myshopify.com", "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME"}
    _write_store(app_config, live_data)
    html = client.get("/demo/acme").get_data(as_text=True)
    assert "Unofficial demo built by Tillbird" not in html


def test_demo_page_shows_expired_page_after_expiry(client, app_config):
    from datetime import datetime, timedelta, timezone

    expired_data = {
        **STORE_DATA,
        "demo_created_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(),
    }
    _write_store(app_config, expired_data)
    response = client.get("/demo/acme")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "expired" in html.lower()
    assert "data-store=" not in html  # widget must not load on an expired demo


def test_demo_page_still_active_before_expiry(client, app_config):
    from datetime import datetime, timezone

    fresh_data = {**STORE_DATA, "demo_created_at": datetime.now(timezone.utc).isoformat()}
    _write_store(app_config, fresh_data)
    html = client.get("/demo/acme").get_data(as_text=True)
    assert 'data-store="acme"' in html
