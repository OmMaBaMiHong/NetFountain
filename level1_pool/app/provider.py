"""供应商客户端：BaseProvider 抽象 + DefaultHttpProvider + ProviderFactory。

本阶段统一假设的供应商响应格式（写入注释供各测试桩/联调沿用）：

    {
      "data": [
        {"ip": "1.2.3.4", "port": 8080, "protocol": "http",
         "region": "CN-Guangdong", "ttl": 120}
      ]
    }

- ``protocol`` 缺省视为 http；``region``/``ttl`` 可空；
- 返回数量不超过请求的 ``count``；
- 网络/解析异常仅记日志并返回空列表。

扩展性：新增供应商只需「继承 BaseProvider + @register("名字")」，主流程零改动。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, ClassVar

import aiohttp

from ip_pool_common.models import Protocol, ProviderIp

from .config import ProviderConfig

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """供应商基类。"""

    name: ClassVar[str] = ""

    def __init__(self, cfg: ProviderConfig, session: aiohttp.ClientSession):
        self.cfg = cfg
        self.session = session

    @abstractmethod
    async def pull(self, count: int) -> list[ProviderIp]:
        """从供应商拉取至多 ``count`` 个 IP。"""

    async def close(self) -> None:
        """释放供应商自有资源；session 生命周期由装配方统一管理，可重复调用。"""


class ProviderFactory:
    """供应商工厂：按 ``type`` 名实例化已注册的供应商子类。"""

    _registry: dict[str, type[BaseProvider]] = {}

    @classmethod
    def register(cls, name: str, provider_cls: type[BaseProvider]) -> None:
        cls._registry[name] = provider_cls

    @classmethod
    def create(
        cls,
        provider_type: str,
        cfg: ProviderConfig,
        session: aiohttp.ClientSession,
    ) -> BaseProvider:
        provider_cls = cls._registry.get(provider_type)
        if provider_cls is None:
            available = ", ".join(sorted(cls._registry)) if cls._registry else "none"
            raise ValueError(
                f"unknown provider type: {provider_type!r} (available: {available})"
            )
        return provider_cls(cfg, session)


def register(name: str):
    """类装饰器：将供应商类型注册进 ProviderFactory，并记录类型名。"""

    def _deco(cls: type[BaseProvider]) -> type[BaseProvider]:
        ProviderFactory.register(name, cls)
        cls.name = name
        return cls

    return _deco


@register("default_http")
class DefaultHttpProvider(BaseProvider):
    """默认 HTTP 供应商：GET api_url（带 api_key），解析统一响应格式。"""

    async def pull(self, count: int) -> list[ProviderIp]:
        params: dict[str, str] = {"count": str(count)}
        if self.cfg.api_key:
            params["api_key"] = self.cfg.api_key
        timeout = aiohttp.ClientTimeout(total=self.cfg.pull_timeout)
        try:
            async with self.session.get(
                self.cfg.api_url, params=params, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json(content_type=None)
            return self._parse(payload, count)
        except asyncio.CancelledError:
            raise
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ValueError,
            TypeError,
            OSError,
        ) as exc:
            logger.warning("pull from %r failed: %s", self.cfg.api_url, exc)
            return []

    def _parse(self, payload: Any, count: int) -> list[ProviderIp]:
        if not isinstance(payload, dict):
            logger.warning("provider payload is not an object: %r", type(payload).__name__)
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            logger.warning("provider payload missing 'data' list")
            return []
        out: list[ProviderIp] = []
        for item in data[:count]:
            parsed = self._parse_item(item)
            if parsed is not None:
                out.append(parsed)
        return out

    def _parse_item(self, item: Any) -> ProviderIp | None:
        if not isinstance(item, dict):
            return None
        ip = item.get("ip")
        if not isinstance(ip, str) or not ip.strip():
            return None
        try:
            port = int(item.get("port"))
        except (TypeError, ValueError):
            return None
        protocol = self._coerce_protocol(item.get("protocol"))
        region = item.get("region")
        ttl: float | None = None
        if item.get("ttl") is not None:
            try:
                ttl = float(item["ttl"])
            except (TypeError, ValueError):
                ttl = None
        return ProviderIp(
            ip=ip,
            port=port,
            protocol=protocol,
            region=region if isinstance(region, str) and region.strip() else None,
            ttl=ttl,
        )

    @staticmethod
    def _coerce_protocol(value: Any) -> Protocol:
        if value is None:
            return Protocol.HTTP
        try:
            return Protocol(str(value).strip().lower())
        except ValueError:
            return Protocol.HTTP
