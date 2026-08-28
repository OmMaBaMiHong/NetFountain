"""models.py 测试：协议枚举、build_proxy_url、各记录默认值。"""
from __future__ import annotations

import pytest

from ip_pool_common.models import (
    IpRecord,
    Level2Record,
    Protocol,
    ProviderIp,
    build_proxy_url,
)


def test_build_proxy_url_all_protocols():
    assert build_proxy_url("1.2.3.4", 8080, Protocol.HTTP) == "http://1.2.3.4:8080"
    assert build_proxy_url("1.2.3.4", 8080, Protocol.HTTPS) == "https://1.2.3.4:8080"
    assert build_proxy_url("1.2.3.4", 8080, Protocol.SOCKS4) == "socks4://1.2.3.4:8080"
    assert build_proxy_url("1.2.3.4", 8080, Protocol.SOCKS5) == "socks5://1.2.3.4:8080"


def test_protocol_members():
    assert [p.value for p in Protocol] == ["http", "https", "socks4", "socks5"]


def test_protocol_invalid_value():
    with pytest.raises(ValueError):
        Protocol("ftp")
    with pytest.raises(ValueError):
        Protocol(123)


def test_ip_record_defaults():
    rec = IpRecord(id=1, ip="1.2.3.4", port=8080, protocol=Protocol.HTTP, proxy_url="http://1.2.3.4:8080")
    assert rec.region is None
    assert rec.ttl is None
    assert rec.created_at == 0.0
    assert rec.last_verified_at == 0.0


def test_level2_record_defaults():
    rec = Level2Record(id=1, ip="1.2.3.4", port=8080, protocol=Protocol.SOCKS5, proxy_url="socks5://1.2.3.4:8080")
    assert rec.region is None
    assert rec.ttl is None
    assert rec.latency_ms == 0.0
    assert rec.leased is False
    assert rec.leased_at is None
    assert rec.created_at == 0.0
    assert rec.last_verified_at == 0.0


def test_provider_ip_defaults():
    p = ProviderIp(ip="1.2.3.4", port=8080, protocol=Protocol.HTTP)
    assert p.region is None
    assert p.ttl is None


def test_provider_ip_ttl_none():
    p = ProviderIp(ip="1.2.3.4", port=8080, protocol=Protocol.HTTP, ttl=None)
    assert p.ttl is None


def test_provider_ip_with_region_and_ttl():
    p = ProviderIp(ip="1.2.3.4", port=8080, protocol=Protocol.HTTPS, region="CN", ttl=30.5)
    assert p.region == "CN"
    assert p.ttl == 30.5