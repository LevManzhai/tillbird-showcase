import sys

import pytest
import requests

from scripts.test_demo import DemoQueryError, ask_demo, main, store_id_from_demo_url


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def test_store_id_from_demo_url():
    assert store_id_from_demo_url("https://app.tillbird.co.uk/demo/demo-abc123") == "demo-abc123"


def test_store_id_from_demo_url_trailing_slash():
    assert store_id_from_demo_url("https://app.tillbird.co.uk/demo/demo-abc123/") == "demo-abc123"


def test_store_id_from_demo_url_no_path_raises():
    with pytest.raises(DemoQueryError):
        store_id_from_demo_url("https://app.tillbird.co.uk/")


def test_ask_demo_posts_expected_payload_and_returns_reply(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse(200, {"reply": "We ship to Ireland in 3-5 days.", "handoff": False})

    monkeypatch.setattr(requests, "post", fake_post)

    reply = ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "Do you ship to Ireland?")

    assert reply == "We ship to Ireland in 3-5 days."
    assert captured["url"] == "https://app.tillbird.co.uk/api/chat"
    assert captured["json"]["store_id"] == "demo-abc123"
    assert captured["json"]["message"] == "Do you ship to Ireland?"
    assert isinstance(captured["json"]["session_id"], str) and captured["json"]["session_id"]


def test_ask_demo_sends_internal_test_header_when_token_configured(monkeypatch):
    """Regression (the actual live bug): manual testing via this script
    through the real /api/chat route ate into a demo's public usage cap,
    so a real prospect opening the link saw "usage limit reached" before
    they ever got a working demo. The header exempts this traffic.
    """
    import scripts.test_demo as test_demo_module

    monkeypatch.setenv("INTERNAL_TEST_TOKEN", "sekrit")
    monkeypatch.setattr(test_demo_module, "INTERNAL_TEST_HEADER", "X-Tillbird-Internal-Test")
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return FakeResponse(200, {"reply": "ok", "handoff": False})

    monkeypatch.setattr(requests, "post", fake_post)
    ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi")

    assert captured["headers"]["X-Tillbird-Internal-Test"] == "sekrit"


def test_ask_demo_omits_internal_test_header_when_no_token_configured(monkeypatch):
    monkeypatch.delenv("INTERNAL_TEST_TOKEN", raising=False)
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return FakeResponse(200, {"reply": "ok", "handoff": False})

    monkeypatch.setattr(requests, "post", fake_post)
    ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi")

    assert captured["headers"] == {}


def test_ask_demo_reuses_given_session_id(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["json"] = json
        return FakeResponse(200, {"reply": "ok", "handoff": False})

    monkeypatch.setattr(requests, "post", fake_post)
    ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi", session_id="fixed-session")
    assert captured["json"]["session_id"] == "fixed-session"


def test_ask_demo_network_failure_raises_demo_query_error(monkeypatch):
    def fake_post(url, json=None, timeout=None, headers=None):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(DemoQueryError):
        ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi")


def test_ask_demo_non_200_raises(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None, headers=None: FakeResponse(404, {"error": "unknown store"}))
    with pytest.raises(DemoQueryError):
        ask_demo("https://app.tillbird.co.uk/demo/demo-missing", "hi")


def test_ask_demo_non_json_response_raises(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None, headers=None: FakeResponse(200, None, text="<html>oops</html>"))
    with pytest.raises(DemoQueryError):
        ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi")


def test_ask_demo_missing_reply_key_raises(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None, headers=None: FakeResponse(200, {"error": "boom"}))
    with pytest.raises(DemoQueryError):
        ask_demo("https://app.tillbird.co.uk/demo/demo-abc123", "hi")


def test_main_prints_reply_and_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None, headers=None: FakeResponse(200, {"reply": "Yes, we ship worldwide.", "handoff": False}))
    monkeypatch.setattr(sys, "argv", ["test_demo.py", "https://app.tillbird.co.uk/demo/demo-abc123", "Do you ship worldwide?"])

    exit_code = main()

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "Yes, we ship worldwide."


def test_main_prints_error_and_returns_one_on_failure(monkeypatch, capsys):
    def fake_post(url, json=None, timeout=None, headers=None):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(sys, "argv", ["test_demo.py", "https://app.tillbird.co.uk/demo/demo-abc123", "hi"])

    exit_code = main()

    assert exit_code == 1
    assert "error" in capsys.readouterr().err
