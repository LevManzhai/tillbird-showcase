"""Process-wide outbound-connection IP family preference.

On some hosts (seen on a Hetzner VPS) a destination resolves to both an
IPv4 and an IPv6 address, but the container has no actual IPv6 route —
and depending on the order socket.getaddrinfo returns those addresses in,
and the exact urllib3/httpx version in use, a request can fail outright
(NewConnectionError: Network is unreachable) instead of quietly falling
back to the IPv4 address, even though a plain IPv4 connection works fine.

This patches socket.getaddrinfo itself (rather than configuring requests
or httpx separately) so every HTTP client built on top of it — requests
(Shopify, legacy scraping), the anthropic SDK (httpx/httpcore
underneath) — is covered by one setting, without needing a per-library
adapter. Controlled by AppConfig.outbound_ip_family / $OUTBOUND_IP_FAMILY.
"""
import socket

_real_getaddrinfo = socket.getaddrinfo
_patched = False

VALID_MODES = ("prefer-ipv4", "ipv4-only", "system")


def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Same results as normal, just with AF_INET entries moved before
    AF_INET6 ones — a stable sort, so relative order within each family
    (e.g. Shopify's own DNS-based load balancing) is unchanged.
    """
    results = _real_getaddrinfo(host, port, family, type, proto, flags)
    return sorted(results, key=lambda r: 0 if r[0] == socket.AF_INET else 1)


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """Drops AF_INET6 entries entirely — falls back to the unfiltered
    result if that would leave nothing (an IPv6-only destination), rather
    than turning a working IPv6-only lookup into a hard failure.
    """
    results = _real_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_only = [r for r in results if r[0] == socket.AF_INET]
    return ipv4_only or results


def apply_outbound_ip_family(mode: str) -> None:
    """Call once at process startup (see app.create_app). "prefer-ipv4"
    (default) sorts IPv4 first but still allows IPv6 as a fallback;
    "ipv4-only" drops IPv6 results outright; "system" restores the
    unpatched stdlib behaviour.
    """
    global _patched
    if mode not in VALID_MODES:
        raise ValueError(f"OUTBOUND_IP_FAMILY must be one of {VALID_MODES}, got {mode!r}")

    if mode == "system":
        socket.getaddrinfo = _real_getaddrinfo
    elif mode == "prefer-ipv4":
        socket.getaddrinfo = _ipv4_first_getaddrinfo
    elif mode == "ipv4-only":
        socket.getaddrinfo = _ipv4_only_getaddrinfo
    _patched = mode != "system"
