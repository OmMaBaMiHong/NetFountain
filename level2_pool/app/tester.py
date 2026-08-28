"""站点测试与周期复验封装。

- ``site_filter``：经代理真实访问目标站点（唯一出口测试），
  ok 且 ``latency < threshold_ms`` 才保留，构造 ``Level2Record``；
- ``revalidate``：仅做代理可达性测试（``proxy_reachability_test``，不测出口），
  返回仍存活项。
"""
from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable

from ip_pool_common.models import IpRecord, Level2Record, build_proxy_url
from ip_pool_common.testing import batch_test, proxy_reachability_test, site_test

SiteTestFn = Callable[[IpRecord], Awaitable[tuple[bool, float]]]
RevalidateFn = Callable[[Level2Record], Awaitable[tuple[bool, float]]]


class Tester:
    """站点连通测试器；可通过 ``site_fn`` / ``revalidate_fn`` 注入替换验证策略。"""

    def __init__(
        self,
        target_url: str,
        threshold_ms: int = 2000,
        connect_timeout: float = 3.0,
        concurrency: int = 20,
        site_fn: SiteTestFn | None = None,
        revalidate_fn: RevalidateFn | None = None,
    ) -> None:
        self.target_url = target_url
        self.threshold_ms = threshold_ms
        self.connect_timeout = connect_timeout
        self.concurrency = concurrency
        self._site_fn = site_fn
        self._revalidate_fn = revalidate_fn

    async def _site(self, rec: IpRecord) -> tuple[bool, float]:
        if self._site_fn is not None:
            res = self._site_fn(rec)
            return await res if inspect.isawaitable(res) else res
        return await site_test(rec.proxy_url, self.target_url, timeout=self.connect_timeout)

    async def _revalidate(self, rec: Level2Record) -> tuple[bool, float]:
        if self._revalidate_fn is not None:
            res = self._revalidate_fn(rec)
            return await res if inspect.isawaitable(res) else res
        return await proxy_reachability_test(
            rec.proxy_url, timeout=self.connect_timeout
        )

    async def site_filter(self, records: list[IpRecord]) -> list[Level2Record]:
        """逐个 site_test 目标站点；ok 且 latency < threshold_ms 才保留并构造 Level2Record。"""
        if not records:
            return []
        sem = asyncio.Semaphore(self.concurrency)

        async def _run(rec: IpRecord) -> tuple[bool, float]:
            async with sem:
                try:
                    return await self._site(rec)
                except Exception:
                    return False, 0.0

        results = await asyncio.gather(*(_run(rec) for rec in records))
        now = time.time()
        result: list[Level2Record] = []
        for rec, (ok, latency) in zip(records, results):
            if ok and latency < self.threshold_ms:
                result.append(
                    Level2Record(
                        id=-1,
                        ip=rec.ip,
                        port=rec.port,
                        protocol=rec.protocol,
                        proxy_url=rec.proxy_url,
                        region=rec.region,
                        ttl=rec.ttl,
                        latency_ms=latency,
                        leased=False,
                        leased_at=None,
                        created_at=now,
                        last_verified_at=now,
                    )
                )
        return result

    async def revalidate(self, records: list[Level2Record]) -> list[Level2Record]:
        """逐个代理可达性测试（不测出口），返回仍存活项，保持原顺序。"""
        if not records:
            return []
        return await batch_test(records, self._revalidate, concurrency=self.concurrency)
