"""后台任务：拉取循环（PullTask）与 TTL 清扫（TtlSweeper）。

鲁棒性：单个 tick 的任何异常仅记日志，循环继续；支持 asyncio 取消优雅退出。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .pool import Level1Pool, ServiceStats
from .provider import BaseProvider
from .tester import Tester

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]


class PullTask:
    """每 ``pull_interval`` 秒拉取一次并做代理可达性测试后入池。

    - 全程持 ``pull_lock``，保证同一时刻只有一次拉取（严格限频）；
    - 单 tick 异常仅记日志，循环继续；
    - 支持 asyncio 取消以优雅关闭。
    """

    def __init__(
        self,
        provider: BaseProvider,
        tester: Tester,
        pool: Level1Pool,
        stats: ServiceStats,
        pull_count: int,
        pull_interval: float,
        pull_lock: asyncio.Lock,
        sleep_fn: SleepFn | None = None,
    ):
        self._provider = provider
        self._tester = tester
        self._pool = pool
        self._stats = stats
        self._pull_count = pull_count
        self._pull_interval = pull_interval
        self._pull_lock = pull_lock
        self._sleep = sleep_fn or asyncio.sleep

    async def run(self) -> None:
        while True:
            try:
                async with self._pull_lock:
                    raw = await self._provider.pull(self._pull_count)
                    passed = await self._tester.test_many(raw)
                    for ip in passed:
                        await self._pool.add(ip, time.time())
                    self._stats.total_pulled += len(raw)
                    self._stats.total_entered += len(passed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pull tick failed")
            await self._sleep(self._pull_interval)


class TtlSweeper:
    """每 ``interval`` 秒清扫一次 TTL 过期项。"""

    def __init__(
        self,
        pool: Level1Pool,
        interval: float,
        sleep_fn: SleepFn | None = None,
    ):
        self._pool = pool
        self._interval = interval
        self._sleep = sleep_fn or asyncio.sleep

    async def run(self) -> None:
        while True:
            await self._sleep(self._interval)
            try:
                await self._pool.sweep_ttl(time.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ttl sweep failed")
