import sys
from datetime import datetime, timezone

import pytest

from app.credential_encryption import encrypt, generate_key
from app.extensions import db
from app.models import ShopifyAppCredential, ShopifyOAuthCredential
from scripts.provision_store import main

KEY = generate_key()


def _insert_credential(app, store_id="acme", shop_domain="acme.myshopify.com"):
    now = datetime.now(timezone.utc)
    with app.app_context():
        db.session.add(
            ShopifyOAuthCredential(
                store_id=store_id,
                shop_domain=shop_domain,
                encrypted_access_token=encrypt("shpat_tok", KEY),
                encrypted_refresh_token=encrypt("shprt_ref", KEY),
                scope="read_orders,read_products",
                access_token_expires_at=now,
                refresh_token_expires_at=now,
            )
        )
        db.session.commit()


def _insert_app_credential(app, store_id="acme", shop_domain="acme.myshopify.com", plan="growth"):
    with app.app_context():
        db.session.add(
            ShopifyAppCredential(
                store_id=store_id,
                shop_domain=shop_domain,
                client_id="client-id",
                encrypted_client_secret=encrypt("secret", KEY),
                plan=plan,
            )
        )
        db.session.commit()


def _run(monkeypatch, app, argv):
    monkeypatch.setattr("scripts.provision_store.create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["provision_store.py"] + argv)
    return main()


def _fake_provision_live_store(captured):
    def _fn(shop_domain, plan, app_config, stores_dir, client=None):
        captured.update(shop_domain=shop_domain, plan=plan, stores_dir=stores_dir, client=client)
        return {"store_id": "acme", "yaml_path": stores_dir / "acme.yaml", "kb_status": "drafted"}

    return _fn


def test_main_errors_when_no_credential_stored(monkeypatch, app, capsys):
    rc = _run(monkeypatch, app, ["acme"])
    assert rc == 1
    assert "no Shopify OAuth credential" in capsys.readouterr().err


def test_main_happy_path_prints_store_id_yaml_status_and_cost(monkeypatch, app, app_config, capsys):
    app_config.anthropic_api_key = ""  # no real API key -> no real client built, cost reported as 0
    _insert_credential(app)
    _insert_app_credential(app)
    captured = {}
    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fake_provision_live_store(captured))

    rc = _run(monkeypatch, app, ["acme"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "store_id: acme" in out
    assert "kb_status: drafted" in out
    assert "cost: $0.0000" in out


def test_main_passes_plan_from_app_credential(monkeypatch, app, app_config):
    _insert_credential(app)
    _insert_app_credential(app, plan="growth")
    captured = {}
    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fake_provision_live_store(captured))

    _run(monkeypatch, app, ["acme"])
    assert captured["plan"] == "growth"
    assert captured["shop_domain"] == "acme.myshopify.com"


def test_main_defaults_plan_when_no_app_credential_row(monkeypatch, app, app_config):
    """Shouldn't normally happen (install requires one), but must not crash."""
    _insert_credential(app)
    captured = {}
    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fake_provision_live_store(captured))

    rc = _run(monkeypatch, app, ["acme"])
    assert rc == 0
    assert captured["plan"] == "starter"


def test_main_overrides_max_cost_from_cli(monkeypatch, app, app_config):
    _insert_credential(app)
    _insert_app_credential(app)
    captured = {}

    def _fn(shop_domain, plan, app_config, stores_dir, client=None):
        captured["max_cost"] = app_config.live_onboarding_max_cost
        return {"store_id": "acme", "yaml_path": stores_dir / "acme.yaml", "kb_status": "skipped_cost_cap"}

    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fn)

    _run(monkeypatch, app, ["acme", "--max-cost", "5.00"])
    assert captured["max_cost"] == 5.00


def test_main_builds_usage_tracking_client_when_api_key_set(monkeypatch, app, app_config):
    app_config.anthropic_api_key = "sk-ant-test"
    _insert_credential(app)
    _insert_app_credential(app)
    monkeypatch.setattr("scripts.provision_store.anthropic.Anthropic", lambda api_key: object())
    captured = {}

    def _fn(shop_domain, plan, app_config, stores_dir, client=None):
        captured["client_is_none"] = client is None
        return {"store_id": "acme", "yaml_path": stores_dir / "acme.yaml", "kb_status": "drafted"}

    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fn)

    _run(monkeypatch, app, ["acme"])
    assert captured["client_is_none"] is False


def test_main_never_prints_token_or_secret(monkeypatch, app, app_config, capsys):
    _insert_credential(app)
    _insert_app_credential(app)
    monkeypatch.setattr("scripts.provision_store.provision_live_store", _fake_provision_live_store({}))

    _run(monkeypatch, app, ["acme"])
    out = capsys.readouterr().out
    assert "shpat_tok" not in out
    assert "shprt_ref" not in out
    assert "secret" not in out


def test_main_requires_store_id_arg(monkeypatch, app):
    monkeypatch.setattr("scripts.provision_store.create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["provision_store.py"])
    with pytest.raises(SystemExit):
        main()
