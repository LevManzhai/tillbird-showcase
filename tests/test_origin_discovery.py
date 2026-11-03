from app.origin_discovery import compute_discovered_origins, merge_allowed_origins


# ---------- compute_discovered_origins ----------


def test_compute_discovered_origins_custom_domain_without_www():
    origins = compute_discovered_origins("acme.co.uk", "acme-fashion.myshopify.com")
    assert origins == ["https://acme.co.uk", "https://www.acme.co.uk", "https://acme-fashion.myshopify.com"]


def test_compute_discovered_origins_custom_domain_with_www():
    origins = compute_discovered_origins("www.acme.co.uk", "acme-fashion.myshopify.com")
    assert origins == ["https://www.acme.co.uk", "https://acme.co.uk", "https://acme-fashion.myshopify.com"]


def test_compute_discovered_origins_no_custom_domain_yet():
    """A brand-new store with no custom domain connected — primaryDomain
    IS the myshopify domain. Must not list it twice, and must NOT invent
    a www counterpart for it (see the dedicated regression test below)."""
    origins = compute_discovered_origins("acme-fashion.myshopify.com", "acme-fashion.myshopify.com")
    assert origins == ["https://acme-fashion.myshopify.com"]


def test_compute_discovered_origins_never_adds_www_for_a_myshopify_host():
    """Regression: confirmed live on tillbird-test (no custom domain
    connected) — a *.myshopify.com host never has a www variant, so
    discovery must not invent "https://www.<store>.myshopify.com".
    """
    origins = compute_discovered_origins("tillbird-test.myshopify.com", "tillbird-test.myshopify.com")
    assert "https://www.tillbird-test.myshopify.com" not in origins
    assert origins == ["https://tillbird-test.myshopify.com"]


def test_compute_discovered_origins_still_adds_www_for_a_real_custom_domain():
    """The www/non-www counterpart is still generated — just never for a
    *.myshopify.com host specifically."""
    origins = compute_discovered_origins("acme.co.uk", "acme.myshopify.com")
    assert origins == ["https://acme.co.uk", "https://www.acme.co.uk", "https://acme.myshopify.com"]


# ---------- merge_allowed_origins ----------


def test_merge_allowed_origins_adds_new_ones():
    merged = merge_allowed_origins(["https://staging.acme.co.uk"], ["https://acme.co.uk", "https://www.acme.co.uk"])
    assert merged == ["https://staging.acme.co.uk", "https://acme.co.uk", "https://www.acme.co.uk"]


def test_merge_allowed_origins_never_drops_an_existing_manual_entry():
    merged = merge_allowed_origins(["https://staging.acme.co.uk"], [])
    assert merged == ["https://staging.acme.co.uk"]


def test_merge_allowed_origins_does_not_duplicate_an_already_present_origin():
    merged = merge_allowed_origins(["https://acme.co.uk"], ["https://acme.co.uk", "https://www.acme.co.uk"])
    assert merged == ["https://acme.co.uk", "https://www.acme.co.uk"]


def test_merge_allowed_origins_empty_existing():
    merged = merge_allowed_origins([], ["https://acme.co.uk"])
    assert merged == ["https://acme.co.uk"]
