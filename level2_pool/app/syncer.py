"""增量同步（含空响应重置）。

- ``Level1SyncClient``：消费一级池 HTTP 契约
  ``GET /api/v1/ips``（全量）、``GET /api/v1/ips/after/{id}``（按 id 增量）；
- ``SyncTask``：每 ``interval`` 秒同步一次；增量返回空时判定一级池重启/换代，
  全量重拉并重置水位线，**绝对不移除池内现存记录**（空闲与租赁均保留），
  旧记录靠复验与 TTL 自然淘汰。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp

from ip_pool_common.models import IpRecord, Protocol, build_proxy_url

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]


class Level1SyncClient:
    """一级池 HTTP 客户端：全量拉取与按 id 增量拉取。"""

    def __init__(
        self,
        base_url: str,
        session: aiohttp.ClientSession,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._timeout = timeout

    async def _get_items(self, url: str) -> list[dict]:
        async with self._session.get(
            url, timeout=aiohttp.ClientTimeout(total=self._timeout)
        ) as resp:
            if resp.status != 200:
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"level1 responded HTTP {resp.status}",
                )
            payload = await resp.json()
        if not isinstance(payload, dict):
            raise ValueError(f"level1 response is not an object: {url}")
        data = payload.get("data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ValueError(f"level1 data is not a list: {url}")
        return data

    @staticmethod
    def _to_ip_record(item: dict) -> IpRecord:
        protocol = Protocol(str(item["protocol"]).lower())
        ip = item["ip"]
        port = int(item["port"])
        return IpRecord(
            id=int(item["id"]),
            ip=ip,
            port=port,
            protocol=protocol,
            proxy_url=item.get("proxy_url") or build_proxy_url(ip, port, protocol),
            region=item.get("region"),
            ttl=item.get("ttl"),
            created_at=item.get("created_at") or 0.0,
            last_verified_at=item.get("last_verified_at") or 0.0,
        )

    async def fetch_all(self) -> list[IpRecord]:
        """GET /api/v1/ips：全量拉取，解析 data 为 IpRecord。"""
        items = await self._get_items(f"{self._base_url}/api/v1/ips")
        return [self._to_ip_record(item) for item in items]

    async def fetch_after(self, id_: int) -> list[IpRecord]:
        """GET /api/v1/ips/after/{id}：增量拉取；data 为空返回 []。"""
        items = await self._get_items(f"{self._base_url}/api/v1/ips/after/{id_}")
        return [self._to_ip_record(item) for item in items]


class SyncTask:
    """增量同步任务：维护 ``last_synced_id`` 水位线，空响应触发全量重拉。"""

    def __init__(
        self,
        client: Level1SyncClient,
        tester: object,
        pool: object,
        stats: object,
        interval: float = 3.0,
        sleep_fn: SleepFn | None = None,
    ) -> None:
        self._client = client
        self._tester = tester
        self._pool = pool
        self._stats = stats
        self._interval = interval
        self._sleep = sleep_fn or asyncio.sleep
        self.last_synced_id: int | None = None

    async def _sync_once(self) -> None:
        if self.last_synced_id is None:
            batch = await self._client.fetch_all()
        else:
            batch = await self._client.fetch_after(self.last_synced_id)
            if not batch:
                batch = await self._client.fetch_all()
        if batch:
            self.last_synced_id = max(r.id for r in batch)
            if self._stats is not None:
                self._stats.last_synced_id = self.last_synced_id
        if self._stats is not None:
            self._stats.total_pulled += len(batch)
        passed = await self._tester.site_filter(batch)
        if self._stats is not None:
            self._stats.total_entered += len(passed)
        for rec in passed:
            await self._pool.upsert(rec)

    async def run(self) -> None:
        """每 ``interval`` 秒循环同步；单 tick 异常仅记日志，不影响池内现有记录；支持取消。"""
        while True:
            try:
                await self._sync_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("sync tick failed")
            await self._sleep(self._interval)