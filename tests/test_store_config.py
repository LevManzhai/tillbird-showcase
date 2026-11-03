import pytest
import yaml

from app.store_config import StoreConfigError, list_store_ids, load_store_config

VALID = {
    "store_id": "acme",
    "store_name": "Acme Fashion",
    "shop_domain": "acme.myshopify.com",
    "public_domain": "acme.co.uk",
    "admin_token_env": "SHOPIFY_ADMIN_TOKEN_ACME",
    "support_email": "support@acme.co.uk",
    "tone": "Friendly",
    "brand_color": "#000000",
    "greeting": "Hi!",
    "hint_text": "Need help?",
    "allowed_origins": ["https://acme.co.uk"],
    "knowledge_base": {
        "shipping": "Royal Mail, 2-3 days.",
        "returns": "30 days.",
        "sizing": "True to size.",
        "faq": [{"question": "Q?", "answer": "A."}],
    },
    "forbidden": ["Never promise refunds."],
    "mode": "live",
}


def _write(tmp_path, store_id, data):
    path = tmp_path / f"{store_id}.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_load_valid_live_store(tmp_path):
    _write(tmp_path, "acme", VALID)
    config = load_store_config("acme", stores_dir=tmp_path)
    assert config.store_id == "acme"
    assert config.mode == "live"
    assert config.knowledge_base.faq[0].question == "Q?"


def test_load_valid_demo_store_without_admin_token(tmp_path):
    data = {**VALID, "store_id": "demo-acme", "mode": "demo", "admin_token_env": None}
    _write(tmp_path, "demo-acme", data)
    config = load_store_config("demo-acme", stores_dir=tmp_path)
    assert config.mode == "demo"


def test_missing_file_raises(tmp_path):
    with pytest.raises(StoreConfigError, match="no config found"):
        load_store_config("nope", stores_dir=tmp_path)


def test_store_id_mismatch_raises(tmp_path):
    data = {**VALID, "store_id": "other"}
    _write(tmp_path, "acme", data)
    with pytest.raises(StoreConfigError, match="does not match filename"):
        load_store_config("acme", stores_dir=tmp_path)


def test_live_mode_without_any_credential_field_means_oauth(tmp_path):
    """A live store with none of admin_token_env/client_id_env/
    client_secret_env set is assumed to use the OAuth credentials table
    instead (see app.shopify_client.build_client_for_store) — this no
    longer raises.
    """
    data = {**VALID, "admin_token_env": None}
    _write(tmp_path, "acme", data)
    config = load_store_config("acme", stores_dir=tmp_path)
    assert config.uses_oauth_credentials() is True


def test_invalid_mode_raises(tmp_path):
    data = {**VALID, "mode": "staging"}
    _write(tmp_path, "acme", data)
    with pytest.raises(StoreConfigError):
        load_store_config("acme", stores_dir=tmp_path)


def test_invalid_shop_domain_raises(tmp_path):
    data = {**VALID, "shop_domain": "acme.com"}
    _write(tmp_path, "acme", data)
    with pytest.raises(StoreConfigError, match="myshopify.com"):
        load_store_config("acme", stores_dir=tmp_path)


def test_demo_mode_allows_non_myshopify_shop_domain(tmp_path):
    # Demo stores are scraped from a public custom domain and never call the
    # real Shopify Admin API, so shop_domain doesn't need to be *.myshopify.com.
    data = {**VALID, "mode": "demo", "admin_token_env": None, "shop_domain": "emberhome.uk"}
    _write(tmp_path, "demo-acme", {**data, "store_id": "demo-acme"})
    config = load_store_config("demo-acme", stores_dir=tmp_path)
    assert config.shop_domain == "emberhome.uk"


def test_demo_expiry(tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.store_config import StoreConfig

    base = {**VALID, "mode": "demo", "admin_token_env": None}

    fresh = StoreConfig.model_validate({**base, "demo_created_at": datetime.now(timezone.utc).isoformat()})
    assert fresh.is_demo_expired(30) is False

    old = StoreConfig.model_validate(
        {**base, "demo_created_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()}
    )
    assert old.is_demo_expired(30) is True

    no_date = StoreConfig.model_validate({**base, "demo_created_at": None})
    assert no_date.is_demo_expired(30) is False

    live_store = StoreConfig.model_validate(
        {**VALID, "demo_created_at": (datetime.now(timezone.utc) - timedelta(days=999)).isoformat()}
    )
    assert live_store.is_demo_expired(30) is False  # live mode never expires


def test_invalid_support_email_raises(tmp_path):
    data = {**VALID, "support_email": "not-an-email"}
    _write(tmp_path, "acme", data)
    with pytest.raises(StoreConfigError, match="support_email"):
        load_store_config("acme", stores_dir=tmp_path)


def test_get_admin_token_reads_named_env_var(tmp_path, monkeypatch):
    _write(tmp_path, "acme", VALID)
    config = load_store_config("acme", stores_dir=tmp_path)
    monkeypatch.setenv("SHOPIFY_ADMIN_TOKEN_ACME", "shpat_test123")
    assert config.get_admin_token() == "shpat_test123"


def test_get_admin_token_missing_env_var_raises(tmp_path, monkeypatch):
    _write(tmp_path, "acme", VALID)
    config = load_store_config("acme", stores_dir=tmp_path)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN_ACME", raising=False)
    with pytest.raises(StoreConfigError, match="not set"):
        config.get_admin_token()


def test_get_admin_token_on_demo_store_raises(tmp_path):
    data = {**VALID, "store_id": "demo-acme", "mode": "demo", "admin_token_env": None}
    _write(tmp_path, "demo-acme", data)
    config = load_store_config("demo-acme", stores_dir=tmp_path)
    with pytest.raises(StoreConfigError, match="not in live mode"):
        config.get_admin_token()


def test_list_store_ids(tmp_path):
    _write(tmp_path, "acme", VALID)
    _write(tmp_path, "demo-acme", {**VALID, "store_id": "demo-acme", "mode": "demo", "admin_token_env": None})
    assert list_store_ids(tmp_path) == ["acme", "demo-acme"]


def test_list_store_ids_missing_dir(tmp_path):
    assert list_store_ids(tmp_path / "nonexistent") == []


def test_real_demo_example_yaml_loads():
    config = load_store_config("demo-example", stores_dir="stores")
    assert config.mode == "demo"
    assert config.store_id == "demo-example"


# ---------- status / trial_ends_at / is_access_blocked ----------


def test_status_unset_is_never_blocked(tmp_path):
    # Every store written before this field existed (e.g. stores/emberhome.yaml)
    # must behave exactly like "active" — deploying this must never go dark.
    from app.store_config import StoreConfig

    store = StoreConfig.model_validate(VALID)
    assert store.is_access_blocked() is False


def test_status_active_is_never_blocked(tmp_path):
    from app.store_config import StoreConfig

    store = StoreConfig.model_validate({**VALID, "status": "active"})
    assert store.is_access_blocked() is False


def test_status_paused_is_blocked(tmp_path):
    from app.store_config import StoreConfig

    store = StoreConfig.model_validate({**VALID, "status": "paused"})
    assert store.is_access_blocked() is True


def test_status_cancelled_is_blocked(tmp_path):
    from app.store_config import StoreConfig

    store = StoreConfig.model_validate({**VALID, "status": "cancelled"})
    assert store.is_access_blocked() is True


def test_status_trial_before_end_date_is_not_blocked(tmp_path):
    from datetime import date, timedelta

    from app.store_config import StoreConfig

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    store = StoreConfig.model_validate({**VALID, "status": "trial", "trial_ends_at": tomorrow})
    assert store.is_access_blocked() is False


def test_status_trial_past_end_date_is_blocked(tmp_path):
    from datetime import date, timedelta

    from app.store_config import StoreConfig

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    store = StoreConfig.model_validate({**VALID, "status": "trial", "trial_ends_at": yesterday})
    assert store.is_access_blocked() is True


def test_status_trial_with_no_end_date_fails_open(tmp_path):
    """A trial with no trial_ends_at set is a config gap, not a reason to
    lock someone out of their own trial."""
    from app.store_config import StoreConfig

    store = StoreConfig.model_validate({**VALID, "status": "trial", "trial_ends_at": None})
    assert store.is_access_blocked() is False


def test_status_never_blocks_a_demo_store(tmp_path):
    """Demo stores keep their own separate is_demo_expired — status/
    trial_ends_at must never apply to them, even if set."""
    from app.store_config import StoreConfig

    demo_data = {**VALID, "mode": "demo", "shop_domain": "acme.example", "admin_token_env": None}
    store = StoreConfig.model_validate({**demo_data, "status": "cancelled"})
    assert store.is_access_blocked() is False


def test_overrides_defaults_to_empty_list(tmp_path):
    # An existing yaml predating overrides (no such key at all) must still
    # load fine rather than failing validation.
    _write(tmp_path, "acme", VALID)
    config = load_store_config("acme", tmp_path)
    assert config.overrides == []


def test_overrides_can_be_set(tmp_path):
    _write(tmp_path, "acme", {**VALID, "overrides": [{"topic": "dispatch time", "answer": "1-2 working days."}]})
    config = load_store_config("acme", tmp_path)
    assert len(config.overrides) == 1
    assert config.overrides[0].topic == "dispatch time"
    assert config.overrides[0].answer == "1-2 working days."


def test_knowledge_base_brand_claims_defaults_to_empty_string(tmp_path):
    # An old demo yaml predating brand_claims (no such key at all) must
    # still load fine rather than failing validation.
    kb = {**VALID["knowledge_base"]}
    kb.pop("brand_claims", None)
    _write(tmp_path, "acme", {**VALID, "knowledge_base": kb})
    config = load_store_config("acme", tmp_path)
    assert config.knowledge_base.brand_claims == ""


def test_knowledge_base_brand_claims_can_be_set(tmp_path):
    _write(
        tmp_path,
        "acme",
        {**VALID, "knowledge_base": {**VALID["knowledge_base"], "brand_claims": "All candles are vegan and pet-safe."}},
    )
    config = load_store_config("acme", tmp_path)
    assert config.knowledge_base.brand_claims == "All candles are vegan and pet-safe."


# ---------- plan / monthly_conversation_limit ----------


def test_plan_and_conversation_limit_default_to_none(tmp_path):
    _write(tmp_path, "acme", VALID)
    config = load_store_config("acme", tmp_path)
    assert config.plan is None
    assert config.monthly_conversation_limit is None


def test_small_plan_defaults_conversation_limit_to_150(tmp_path):
    _write(tmp_path, "acme", {**VALID, "plan": "small"})
    config = load_store_config("acme", tmp_path)
    assert config.monthly_conversation_limit == 150


def test_starter_plan_defaults_conversation_limit_to_500(tmp_path):
    _write(tmp_path, "acme", {**VALID, "plan": "starter"})
    config = load_store_config("acme", tmp_path)
    assert config.monthly_conversation_limit == 500


def test_growth_plan_defaults_conversation_limit_to_1500(tmp_path):
    _write(tmp_path, "acme", {**VALID, "plan": "growth"})
    config = load_store_config("acme", tmp_path)
    assert config.monthly_conversation_limit == 1500


def test_explicit_conversation_limit_overrides_plan_default(tmp_path):
    _write(tmp_path, "acme", {**VALID, "plan": "starter", "monthly_conversation_limit": 750})
    config = load_store_config("acme", tmp_path)
    assert config.monthly_conversation_limit == 750


def test_invalid_plan_value_raises(tmp_path):
    _write(tmp_path, "acme", {**VALID, "plan": "enterprise"})
    with pytest.raises(StoreConfigError):
        load_store_config("acme", tmp_path)
