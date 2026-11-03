import pytest
import yaml

from app.config import AppConfig
from app.demo_kb import StoreProvisioningError
from app.live_onboarding import provision_live_store, store_id_for_shop
from app.store_config import load_store_config

# ---------- store_id_for_shop ----------


def test_store_id_for_shop_strips_myshopify_suffix():
    assert store_id_for_shop("acme-fashion.myshopify.com") == "acme-fashion"


def test_store_id_for_shop_is_stable_across_calls():
    assert store_id_for_shop("acme-fashion.myshopify.com") == store_id_for_shop("acme-fashion.myshopify.com")


def test_store_id_for_shop_sanitises_unexpected_characters():
    assert store_id_for_shop("Acme_Fashion!!.myshopify.com") == "acme-fashion"


# ---------- provision_live_store ----------


def _app_config(tmp_path, max_cost=1.00, anthropic_key="sk-ant-test"):
    config = AppConfig()
    config.live_onboarding_max_cost = max_cost
    config.anthropic_api_key = anthropic_key
    return config


def test_provision_live_store_writes_pending_review_yaml(tmp_path, monkeypatch):
    drafted = {
        "base_url": "https://acme.myshopify.com",
        "host": "acme.myshopify.com",
        "shop_name": "Acme Fashion",
        "brand_color": "#ff0000",
        "products": [],
        "policy_texts": {"shipping": "text"},
        "draft": {"tone": "Warm.", "shipping": "Free UK shipping.", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
    }
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: drafted)

    result = provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)

    assert result["store_id"] == "acme"
    assert result["kb_status"] == "drafted"
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.mode == "live"
    assert store.live_status == "pending_review"
    assert store.plan == "starter"
    assert store.store_name == "Acme Fashion"
    assert store.knowledge_base.shipping == "Free UK shipping."
    assert store.uses_oauth_credentials() is True


def test_provision_live_store_carries_small_plan_through_into_the_yaml(tmp_path, monkeypatch):
    """The new cheaper plan: plan="small" set via scripts/add_client_app.py
    (read back from ShopifyAppCredential.plan by scripts/provision_store.py)
    must survive all the way into stores/<id>.yaml with its own
    monthly_conversation_limit default (150) — not just starter/growth.
    """
    drafted = {
        "base_url": "https://acme.myshopify.com",
        "host": "acme.myshopify.com",
        "shop_name": "Acme Fashion",
        "brand_color": "#ff0000",
        "products": [],
        "policy_texts": {},
        "draft": {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
    }
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: drafted)

    provision_live_store("acme.myshopify.com", "small", _app_config(tmp_path), stores_dir=tmp_path)

    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.plan == "small"
    assert store.monthly_conversation_limit == 150


def test_provision_live_store_rejects_an_unknown_plan_before_writing_anything(tmp_path, monkeypatch):
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not scrape for a rejected plan")))

    with pytest.raises(ValueError, match="plan"):
        provision_live_store("acme.myshopify.com", "enterprise", _app_config(tmp_path), stores_dir=tmp_path)

    assert not (tmp_path / "acme.yaml").exists()


def test_provision_live_store_skips_draft_when_over_cost_cap(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: called.append(1))

    result = provision_live_store("acme.myshopify.com", "growth", _app_config(tmp_path, max_cost=0.0), stores_dir=tmp_path)

    assert called == []
    assert result["kb_status"] == "skipped_cost_cap"
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.live_status == "pending_review"
    assert store.knowledge_base.shipping == ""


def test_provision_live_store_falls_back_cleanly_when_scraping_fails(tmp_path, monkeypatch):
    def _raise(*a, **k):
        raise StoreProvisioningError("not a Shopify store")

    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", _raise)

    result = provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)

    assert result["kb_status"].startswith("failed:")
    # The install/YAML-write itself must still succeed even though drafting failed.
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.mode == "live"
    assert store.live_status == "pending_review"


def test_provision_live_store_is_idempotent_on_store_id(tmp_path, monkeypatch):
    """A reinstall lands on the same store_id/YAML file, not a duplicate."""
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: (_ for _ in ()).throw(StoreProvisioningError("x")))

    first = provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)
    second = provision_live_store("acme.myshopify.com", "growth", _app_config(tmp_path), stores_dir=tmp_path)

    assert first["store_id"] == second["store_id"] == "acme"
    assert first["yaml_path"] == second["yaml_path"]
    with first["yaml_path"].open() as f:
        data = yaml.safe_load(f)
    assert data["plan"] == "growth"  # the second call's plan wins


def test_provision_live_store_skips_already_reviewed_store(tmp_path, monkeypatch):
    """Re-running provisioning (e.g. the background job spawned by a
    merchant reopening the install link to refresh scopes) must never
    overwrite a config a human has already reviewed/approved — or one
    deliberately deactivated.
    """
    called = []
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: called.append(1))

    yaml_path = tmp_path / "acme.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "store_id": "acme",
                "store_name": "Acme Fashion — hand-edited",
                "shop_domain": "acme.myshopify.com",
                "public_domain": "acme.myshopify.com",
                "support_email": "support@acme.co.uk",
                "tone": "Friendly",
                "brand_color": "#000000",
                "greeting": "Hi!",
                "hint_text": "Need help?",
                "allowed_origins": [],
                "knowledge_base": {"shipping": "Hand-written shipping info.", "returns": "", "sizing": "", "faq": []},
                "forbidden": [],
                "mode": "live",
                "live_status": "active",
            }
        )
    )

    result = provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)

    assert called == []
    assert result["kb_status"] == "skipped_already_provisioned"
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.live_status == "active"
    assert store.store_name == "Acme Fashion — hand-edited"
    assert store.knowledge_base.shipping == "Hand-written shipping info."


def test_provision_live_store_redrafts_when_still_pending_review(tmp_path, monkeypatch):
    """Unlike an already-reviewed store, one still sitting at
    pending_review (e.g. a crashed first attempt) is fine to re-draft."""
    drafted = {
        "base_url": "https://acme.myshopify.com",
        "host": "acme.myshopify.com",
        "shop_name": "Acme Fashion",
        "brand_color": "#ff0000",
        "products": [],
        "policy_texts": {},
        "draft": {"tone": "Warm.", "shipping": "Free UK shipping.", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
    }
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: drafted)

    yaml_path = tmp_path / "acme.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "store_id": "acme",
                "store_name": "acme.myshopify.com",
                "shop_domain": "acme.myshopify.com",
                "public_domain": "acme.myshopify.com",
                "support_email": "support@acme.myshopify.com",
                "tone": "Friendly",
                "brand_color": "#1a1a2e",
                "greeting": "Hi!",
                "hint_text": "Need help?",
                "allowed_origins": [],
                "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
                "forbidden": [],
                "mode": "live",
                "live_status": "pending_review",
            }
        )
    )

    result = provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)

    assert result["kb_status"] == "drafted"
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.store_name == "Acme Fashion"
    assert store.knowledge_base.shipping == "Free UK shipping."


def test_provision_live_store_preserves_overrides_across_a_redraft(tmp_path, monkeypatch):
    """B4 spec: a manual correction must never be wiped by a scrape/
    refresh — including a pending_review re-draft (e.g. a crashed first
    attempt retried), which otherwise rewrites the whole yaml from
    scratch.
    """
    drafted = {
        "base_url": "https://acme.myshopify.com",
        "host": "acme.myshopify.com",
        "shop_name": "Acme Fashion",
        "brand_color": "#ff0000",
        "products": [],
        "policy_texts": {},
        "draft": {"tone": "Warm.", "shipping": "Dispatched within 2-3 working days.", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
    }
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: drafted)

    yaml_path = tmp_path / "acme.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "store_id": "acme",
                "store_name": "acme.myshopify.com",
                "shop_domain": "acme.myshopify.com",
                "public_domain": "acme.myshopify.com",
                "support_email": "support@acme.myshopify.com",
                "tone": "Friendly",
                "brand_color": "#1a1a2e",
                "greeting": "Hi!",
                "hint_text": "Need help?",
                "allowed_origins": [],
                "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
                "overrides": [{"topic": "dispatch time", "answer": "We dispatch within 1-2 working days."}],
                "forbidden": [],
                "mode": "live",
                "live_status": "pending_review",
            }
        )
    )

    provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path)

    store = load_store_config("acme", stores_dir=tmp_path)
    assert len(store.overrides) == 1
    assert store.overrides[0].topic == "dispatch time"
    assert store.overrides[0].answer == "We dispatch within 1-2 working days."


def test_provision_live_store_forwards_client_to_scrape_and_draft_store(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "app.live_onboarding.scrape_and_draft_store",
        lambda store_url, app_config, client=None: captured.update(client=client)
        or {
            "base_url": store_url,
            "host": "acme.myshopify.com",
            "shop_name": "Acme",
            "brand_color": "#fff",
            "products": [],
            "policy_texts": {},
            "draft": {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
        },
    )
    sentinel = object()
    provision_live_store("acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path, client=sentinel)
    assert captured["client"] is sentinel


# ---------- C4: allowed_origins auto-discovery ----------


class _FakeShopifyClient:
    def __init__(self, primary_host, myshopify_domain):
        self._primary_host = primary_host
        self._myshopify_domain = myshopify_domain

    def get_shop_domains(self):
        return {"primary_host": self._primary_host, "myshopify_domain": self._myshopify_domain}


class _FailingShopifyClient:
    def get_shop_domains(self):
        raise RuntimeError("Shopify API down")


def _drafted(**overrides):
    base = {
        "base_url": "https://acme.myshopify.com",
        "host": "acme.myshopify.com",
        "shop_name": "Acme Fashion",
        "brand_color": "#ff0000",
        "products": [],
        "policy_texts": {},
        "draft": {"tone": "", "shipping": "", "returns": "", "sizing": "", "brand_claims": "", "faq": []},
    }
    return {**base, **overrides}


def test_provision_live_store_discovers_allowed_origins(tmp_path, monkeypatch):
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: _drafted())
    fake_shopify = _FakeShopifyClient("acme.co.uk", "acme-fashion.myshopify.com")

    result = provision_live_store(
        "acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path, shopify_client=fake_shopify
    )

    assert result["origins_status"] == "discovered"
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.allowed_origins == [
        "https://acme.co.uk",
        "https://www.acme.co.uk",
        "https://acme-fashion.myshopify.com",
    ]


def test_provision_live_store_handles_primary_domain_already_being_myshopify(tmp_path, monkeypatch):
    """A brand-new store with no custom domain yet — primaryDomain.host
    and myshopifyDomain are the same string, must not duplicate it, and
    must not invent a www counterpart for a *.myshopify.com host (see the
    tillbird-test regression test in tests/test_origin_discovery.py)."""
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: _drafted())
    fake_shopify = _FakeShopifyClient("acme-fashion.myshopify.com", "acme-fashion.myshopify.com")

    provision_live_store(
        "acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path, shopify_client=fake_shopify
    )

    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.allowed_origins == ["https://acme-fashion.myshopify.com"]


def test_provision_live_store_origin_discovery_failure_never_fails_the_install(tmp_path, monkeypatch):
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: _drafted())

    result = provision_live_store(
        "acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path, shopify_client=_FailingShopifyClient()
    )

    assert result["origins_status"].startswith("failed:")
    store = load_store_config("acme", stores_dir=tmp_path)
    assert store.mode == "live"  # the install itself still succeeded
    assert store.allowed_origins == []


def test_provision_live_store_redraft_preserves_manually_added_origin_and_adds_discovered_ones(tmp_path, monkeypatch):
    """A re-draft (still pending_review) must keep an origin Lev added by
    hand, never overwrite it — only ADD newly-discovered ones."""
    monkeypatch.setattr("app.live_onboarding.scrape_and_draft_store", lambda *a, **k: _drafted())

    yaml_path = tmp_path / "acme.yaml"
    yaml_path.write_text(
        yaml.safe_dump(
            {
                "store_id": "acme",
                "store_name": "acme.myshopify.com",
                "shop_domain": "acme.myshopify.com",
                "public_domain": "acme.myshopify.com",
                "support_email": "support@acme.myshopify.com",
                "tone": "Friendly",
                "brand_color": "#1a1a2e",
                "greeting": "Hi!",
                "hint_text": "Need help?",
                "allowed_origins": ["https://staging.acme.co.uk"],
                "knowledge_base": {"shipping": "", "returns": "", "sizing": "", "faq": []},
                "forbidden": [],
                "mode": "live",
                "live_status": "pending_review",
            }
        )
    )
    fake_shopify = _FakeShopifyClient("acme.co.uk", "acme-fashion.myshopify.com")

    provision_live_store(
        "acme.myshopify.com", "starter", _app_config(tmp_path), stores_dir=tmp_path, shopify_client=fake_shopify
    )

    store = load_store_config("acme", stores_dir=tmp_path)
    assert "https://staging.acme.co.uk" in store.allowed_origins
    assert "https://acme.co.uk" in store.allowed_origins
    assert "https://acme-fashion.myshopify.com" in store.allowed_origins
