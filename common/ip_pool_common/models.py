"""数据模型：协议枚举、供应商输出、一级/二级池记录。

供三个业务项目（一级池、二级池、代理层）共享使用的纯数据结构，
不包含任何业务逻辑。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Protocol(StrEnum):
    """代理协议枚举。"""

    HTTP = "http"
    HTTPS = "https"
    SOCKS4 = "socks4"
    SOCKS5 = "socks5"


@dataclass
class ProviderIp:
    """供应商规范化输出（一级池 BaseProvider 用）。

    供应商响应解析后的统一结构，ip/port/protocol 为必填，
    region 与 ttl 为可选（供应商支持 TTL 时 ttl 为秒）。
    """

    ip: str
    port: int
    protocol: Protocol
    region: str | None = None
    ttl: float | None = None


@dataclass
class IpRecord:
    """一级池记录。"""

    id: int
    ip: str
    port: int
    protocol: Protocol
    proxy_url: str
    region: str | None = None
    ttl: float | None = None
    created_at: float = 0.0
    last_verified_at: float = 0.0


@dataclass
class Level2Record:
    """二级池记录（唯一键 = proxy_url；id 为二级池本地自增）。"""

    id: int
    ip: str
    port: int
    protocol: Protocol
    proxy_url: str
    region: str | None = None
    ttl: float | None = None
    latency_ms: float = 0.0
    leased: bool = False
    leased_at: float | None = None
    created_at: float = 0.0
    last_verified_at: float = 0.0


def build_proxy_url(ip: str, port: int, protocol: Protocol) -> str:
    """组装 proxy_url，如 `http://1.2.3.4:8080`。"""
    return f"{protocol.value}://{ip}:{port}"