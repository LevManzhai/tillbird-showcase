import sys

import pytest

import app.store_config as store_config_module
from scripts.ask_agent import main

BASE_STORE = {
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
    "knowledge_base": {"shipping": "", "returns": "30 days.", "sizing": "", "faq": []},
    "forbidden": [],
    "mode": "demo",
}


def _run(monkeypatch, capsys, argv, app_config_api_key="sk-ant-test"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", app_config_api_key)
    old_argv = sys.argv
    try:
        sys.argv = ["ask_agent.py"] + argv
        rc = main()
    finally:
        sys.argv = old_argv
    return rc, capsys.readouterr().out


def test_ask_agent_prints_a_single_reply(monkeypatch, capsys, tmp_path):
    store = store_config_module.StoreConfig.model_validate(BASE_STORE)
    monkeypatch.setattr("scripts.ask_agent.load_store_config", lambda store_id, stores_dir: store)
    monkeypatch.setattr("scripts.ask_agent.run_agent_turn", lambda app_config, sc, history, question: _FakeResult("30 days, no problem."))

    rc, out = _run(monkeypatch, capsys, ["acme", "Can I return this?"])
    assert rc == 0
    assert out.strip() == "30 days, no problem."


def test_ask_agent_repeats_and_numbers_each_run(monkeypatch, capsys):
    store = store_config_module.StoreConfig.model_validate(BASE_STORE)
    monkeypatch.setattr("scripts.ask_agent.load_store_config", lambda store_id, stores_dir: store)
    replies = iter(["first", "second", "third"])
    monkeypatch.setattr(
        "scripts.ask_agent.run_agent_turn", lambda app_config, sc, history, question: _FakeResult(next(replies))
    )

    rc, out = _run(monkeypatch, capsys, ["acme", "Can I return this?", "--repeat", "3"])
    assert rc == 0
    lines = out.strip().splitlines()
    assert lines == ["[1/3] first", "[2/3] second", "[3/3] third"]


def test_ask_agent_errors_cleanly_for_unknown_store(monkeypatch, capsys):
    from app.store_config import StoreConfigError

    def _raise(store_id, stores_dir):
        raise StoreConfigError(f"no config found for store {store_id!r}")

    monkeypatch.setattr("scripts.ask_agent.load_store_config", _raise)
    rc, _ = _run(monkeypatch, capsys, ["nope", "hi"])
    assert rc == 1


def test_ask_agent_requires_anthropic_api_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    old_argv = sys.argv
    try:
        sys.argv = ["ask_agent.py", "acme", "hi"]
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 1


class _FakeResult:
    def __init__(self, reply):
        self.reply = reply
