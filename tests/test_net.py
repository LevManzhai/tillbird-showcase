import socket

import pytest

from app import net


def _fake_results():
    return [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2620:127:f00f:e::", 443, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("23.227.38.74", 443)),
    ]


@pytest.fixture(autouse=True)
def _restore_getaddrinfo():
    original = socket.getaddrinfo
    yield
    socket.getaddrinfo = original


def test_ipv4_first_sorts_ipv4_before_ipv6(monkeypatch):
    monkeypatch.setattr(net, "_real_getaddrinfo", lambda *a, **k: _fake_results())
    results = net._ipv4_first_getaddrinfo("tillbird-test.myshopify.com", 443)
    assert [r[0] for r in results] == [socket.AF_INET, socket.AF_INET6]


def test_ipv4_first_is_stable_within_a_family(monkeypatch):
    ipv4_a = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))
    ipv4_b = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("2.2.2.2", 443))
    monkeypatch.setattr(net, "_real_getaddrinfo", lambda *a, **k: [ipv4_a, ipv4_b])
    results = net._ipv4_first_getaddrinfo("host", 443)
    assert results == [ipv4_a, ipv4_b]


def test_ipv4_only_drops_ipv6_entries(monkeypatch):
    monkeypatch.setattr(net, "_real_getaddrinfo", lambda *a, **k: _fake_results())
    results = net._ipv4_only_getaddrinfo("tillbird-test.myshopify.com", 443)
    assert [r[0] for r in results] == [socket.AF_INET]


def test_ipv4_only_falls_back_to_full_results_for_ipv6_only_host(monkeypatch):
    ipv6_only = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))]
    monkeypatch.setattr(net, "_real_getaddrinfo", lambda *a, **k: ipv6_only)
    results = net._ipv4_only_getaddrinfo("v6only.example.com", 443)
    assert results == ipv6_only


def test_apply_prefer_ipv4_patches_socket_getaddrinfo():
    net.apply_outbound_ip_family("prefer-ipv4")
    assert socket.getaddrinfo is net._ipv4_first_getaddrinfo


def test_apply_ipv4_only_patches_socket_getaddrinfo():
    net.apply_outbound_ip_family("ipv4-only")
    assert socket.getaddrinfo is net._ipv4_only_getaddrinfo


def test_apply_system_restores_real_getaddrinfo():
    net.apply_outbound_ip_family("prefer-ipv4")
    net.apply_outbound_ip_family("system")
    assert socket.getaddrinfo is net._real_getaddrinfo


def test_apply_rejects_unknown_mode():
    with pytest.raises(ValueError, match="OUTBOUND_IP_FAMILY"):
        net.apply_outbound_ip_family("dual-stack")
