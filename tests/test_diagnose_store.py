import sys

import pytest

from app.catalog import CatalogError
from app.store_config import StoreConfigError
from scripts.diagnose_store import format_diagnostics_report, main

FORBIDDEN_SUBSTRINGS = ("jane@example.com", "@example.com", "shpat_", "secret")


def _diagnostics(orders=None, scopes=("read_orders", "read_products"), errors=None):
    if orders is None:
        orders = [{"name": "#2003", "created_at": "2026-09-22T10:00:00Z", "email_present": True}]
    return {"orders": orders, "scopes": list(scopes), "errors": errors or []}


# ---------- format_diagnostics_report ----------


def test_report_happy_path_lists_orders_and_scopes():
    report = format_diagnostics_report(_diagnostics())
    assert "read_orders, read_products" in report
    assert "#2003" in report
    assert "2026-09-22T10:00:00Z" in report
    assert "email present: True" in report
    assert "PCD" not in report and "protected customer data" not in report.lower()


def test_report_never_contains_email_value():
    report = format_diagnostics_report(_diagnostics())
    assert "jane@example.com" not in report


def test_report_no_orders():
    report = format_diagnostics_report(_diagnostics(orders=[]))
    assert "No orders returned." in report


def test_report_missing_email_triggers_guidance():
    orders = [{"name": "#2004", "created_at": "2026-09-22T10:00:00Z", "email_present": False}]
    report = format_diagnostics_report(_diagnostics(orders=orders))
    assert "email present: False" in report
    assert "Dev Dashboard" in report
    assert "shopify app deploy" in report


def test_report_graphql_errors_trigger_guidance_and_are_shown():
    errors = [{"message": "This action requires merchant approval for protected customer data"}]
    report = format_diagnostics_report(_diagnostics(errors=errors))
    assert "requires merchant approval" in report
    assert "Dev Dashboard" in report


def test_report_no_errors_and_all_emails_present_has_no_guidance():
    report = format_diagnostics_report(_diagnostics())
    assert "Dev Dashboard" not in report


# ---------- main() ----------


class _FakeClient:
    def __init__(self, diagnostics):
        self._diagnostics = diagnostics

    def get_diagnostics(self):
        return self._diagnostics


def _run(monkeypatch, app, argv, store=object(), client=None, load_error=None, build_error=None):
    monkeypatch.setattr("scripts.diagnose_store.create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["diagnose_store.py"] + argv)

    def fake_load_store_config(store_id, stores_dir):
        if load_error:
            raise load_error
        return store

    def fake_build_client_for_store(store_config, app_config):
        if build_error:
            raise build_error
        return client or _FakeClient(_diagnostics())

    monkeypatch.setattr("scripts.diagnose_store.load_store_config", fake_load_store_config)
    monkeypatch.setattr("scripts.diagnose_store.build_client_for_store", fake_build_client_for_store)
    return main()


def test_main_happy_path_prints_report(monkeypatch, app, capsys):
    rc = _run(monkeypatch, app, ["tillbird-test"], client=_FakeClient(_diagnostics()))
    assert rc == 0
    out = capsys.readouterr().out
    assert "#2003" in out
    assert "read_orders, read_products" in out


def test_main_never_prints_pii_or_secrets(monkeypatch, app, capsys):
    orders = [{"name": "#2005", "created_at": "2026-09-22T10:00:00Z", "email_present": False}]
    rc = _run(monkeypatch, app, ["tillbird-test"], client=_FakeClient(_diagnostics(orders=orders)))
    assert rc == 0
    out = capsys.readouterr().out
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden not in out


def test_main_errors_on_bad_store_config(monkeypatch, app, capsys):
    rc = _run(monkeypatch, app, ["nope"], load_error=StoreConfigError("no such store 'nope'"))
    assert rc == 1
    assert "no such store" in capsys.readouterr().err


def test_main_errors_when_client_build_fails(monkeypatch, app, capsys):
    rc = _run(monkeypatch, app, ["tillbird-test"], build_error=CatalogError("needs reauthorization"))
    assert rc == 1
    assert "needs reauthorization" in capsys.readouterr().err


def test_main_errors_when_get_diagnostics_fails(monkeypatch, app, capsys):
    class _FailingClient:
        def get_diagnostics(self):
            raise CatalogError("Shopify API request failed: timeout")

    rc = _run(monkeypatch, app, ["tillbird-test"], client=_FailingClient())
    assert rc == 1
    assert "timeout" in capsys.readouterr().err


def test_main_requires_store_id_arg(monkeypatch, app):
    monkeypatch.setattr("scripts.diagnose_store.create_app", lambda: app)
    monkeypatch.setattr(sys, "argv", ["diagnose_store.py"])
    with pytest.raises(SystemExit):
        main()
