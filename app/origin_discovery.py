"""Computing a store's allowed_origins from what Shopify's own Admin API
says its domains are — used at provisioning time (app.live_onboarding)
and by scripts/refresh_store_origins.py (a client connecting a custom
domain after the fact). Pure functions, no network/DB — see
app.shopify_client.ShopifyAdminClient.get_shop_domains for the fetch.
"""


def compute_discovered_origins(primary_host: str, myshopify_domain: str) -> list[str]:
    """[https://<primary host>, https://<its www/non-www counterpart>,
    https://<myshopify domain>], deduplicated (a brand-new store with no
    custom domain yet has primary_host == myshopify_domain) and in this
    order. e.g. primary_host="acme.co.uk" ->
    ["https://acme.co.uk", "https://www.acme.co.uk", "https://acme.myshopify.com"]

    The www/non-www counterpart is only generated for a real custom
    domain — a *.myshopify.com host never has one (confirmed live:
    discovery on tillbird-test, a store with no custom domain connected,
    added the nonsensical "https://www.tillbird-test.myshopify.com").
    """
    hosts = [primary_host]
    if not primary_host.endswith(".myshopify.com"):
        hosts.append(primary_host[4:] if primary_host.startswith("www.") else f"www.{primary_host}")
    hosts.append(myshopify_domain)

    seen: set[str] = set()
    origins = []
    for host in hosts:
        origin = f"https://{host}"
        if origin not in seen:
            seen.add(origin)
            origins.append(origin)
    return origins


def merge_allowed_origins(existing: list[str], discovered: list[str]) -> list[str]:
    """Adds any of `discovered` not already in `existing`, preserving
    `existing`'s own order/entries exactly — an origin added by hand
    (e.g. for a dev/staging domain) is never removed just because a later
    discovery run didn't happen to find it too.
    """
    merged = list(existing)
    for origin in discovered:
        if origin not in merged:
            merged.append(origin)
    return merged
