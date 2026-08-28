"""tasks.py 测试：限频 / 异常隔离 / 统计累加 / TTL 清扫 / 取消。"""
from __future__ import annotations

import asyncio
import time

import pytest

from app.pool import Level1Pool, ServiceStats
from app.tasks import PullTask, TtlSweeper


class _StopLoop(Exception):
    pass


class _SleepRecorder:
    """记录每次 sleep 时长，可设置 stop_after 次后抛 _StopLoop 终止循环。"""

    def __init__(self, stop_after: int | None = None):
        self.durations: list[float] = []
        self._stop_after = stop_after

    async def __call__(self, duration: float):
        self.durations.append(duration)
        if self._stop_after is not None and len(self.durations) >= self._stop_after:
            raise _StopLoop()


class _FakeProvider:
    def __init__(self, responses):
        self._responses = responses
        self.pull_calls = 0

    async def pull(self, count):
        idx = min(self.pull_calls, len(self._responses) - 1)
        resp = self._responses[idx]
        self.pull_calls += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


class _FakeTester:
    def __init__(self, passed_count):
        self._passed_count = passed_count

    async def test_many(self, ips):
        return ips[: self._passed_count]


async def test_pull_rate_limit_sleeps_pull_interval(make_ip):
    provider = _FakeProvider([[make_ip(1), make_ip(2)]])
    tester = _FakeTester(passed_count=2)
    pool = Level1Pool(max_size=100)
    stats = ServiceStats()
    lock = asyncio.Lock()
    sleep_rec = _SleepRecorder(stop_after=2)
    task = PullTask(provider, tester, pool, stats, 10, 1.5, lock, sleep_fn=sleep_rec)
    with pytest.raises(_StopLoop):
        await task.run()
    assert sleep_rec.durations == [1.5, 1.5]
    assert provider.pull_calls == 2
    assert pool.size() == 4  # 2 ticks × 2 条


async def test_exception_in_tick_does_not_stop_loop(make_ip):
    provider = _FakeProvider(
        [RuntimeError("boom"), [make_ip(1)], [make_ip(2)], [make_ip(3)]]
    )
    tester = _FakeTester(passed_count=1)
    pool = Level1Pool(max_size=100)
    stats = ServiceStats()
    lock = asyncio.Lock()
    sleep_rec = _SleepRecorder(stop_after=3)
    task = PullTask(provider, tester, pool, stats, 10, 0.01, lock, sleep_fn=sleep_rec)
    with pytest.raises(_StopLoop):
        await task.run()
    assert provider.pull_calls == 3
    assert pool.size() == 2
    assert stats.total_pulled == 2
    assert stats.total_entered == 2


async def test_stats_accumulate_correctly(make_ip):
    provider = _FakeProvider([[make_ip(1), make_ip(2), make_ip(3)], [make_ip(4)]])
    tester = _FakeTester(passed_count=2)
    pool = Level1Pool(max_size=100)
    stats = ServiceStats()
    lock = asyncio.Lock()
    sleep_rec = _SleepRecorder(stop_after=2)
    task = PullTask(provider, tester, pool, stats, 10, 0.01, lock, sleep_fn=sleep_rec)
    with pytest.raises(_StopLoop):
        await task.run()
    assert stats.total_pulled == 4
    assert stats.total_entered == 3
    assert pool.size() == 3


async def test_pull_task_cancellation(make_ip):
    provider = _FakeProvider([[make_ip(1)]])
    tester = _FakeTester(passed_count=1)
    pool = Level1Pool(max_size=100)
    stats = ServiceStats()
    lock = asyncio.Lock()

    async def _never(duration):
        await asyncio.sleep(3600)

    task = PullTask(provider, tester, pool, stats, 10, 1.0, lock, sleep_fn=_never)
    t = asyncio.create_task(task.run())
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert pool.size() == 1


async def test_ttl_sweeper_removes_expired(make_ip):
    pool = Level1Pool(max_size=100)
    now = time.time()
    await pool.add(make_ip(1, ttl=5), now - 10)     # 过期
    await pool.add(make_ip(2, ttl=1000), now - 10)  # 未过期
    sleep_rec = _SleepRecorder(stop_after=2)
    sweeper = TtlSweeper(pool, 5.0, sleep_fn=sleep_rec)
    with pytest.raises(_StopLoop):
        await sweeper.run()
    remaining = await pool.all()
    assert len(remaining) == 1
    assert remaining[0].id == 1


async def test_ttl_sweeper_exception_isolation(make_ip):
    pool = Level1Pool(max_size=100)
    now = time.time()
    await pool.add(make_ip(1, ttl=5), now - 10)
    await pool.add(make_ip(2, ttl=1000), now - 10)

    calls = []
    original_sweep = pool.sweep_ttl

    async def _flaky_sweep(now_):
        calls.append(now_)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await original_sweep(now_)

    pool.sweep_ttl = _flaky_sweep
    sleep_rec = _SleepRecorder(stop_after=3)
    sweeper = TtlSweeper(pool, 5.0, sleep_fn=sleep_rec)
    with pytest.raises(_StopLoop):
        await sweeper.run()
    assert len(calls) == 2
    remaining = await pool.all()
    assert len(remaining) == 1
    assert remaining[0].id == 1


async def test_ttl_sweeper_cancellation(make_ip):
    pool = Level1Pool(max_size=100)

    async def _never(duration):
        await asyncio.sleep(3600)

    sweeper = TtlSweeper(pool, 5.0, sleep_fn=_never)
    t = asyncio.create_task(sweeper.run())
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t


async def test_ttl_sweeper_cancelled_during_sweep(make_ip):
    pool = Level1Pool(max_size=100)

    async def _cancel_sweep(now_):
        raise asyncio.CancelledError()

    pool.sweep_ttl = _cancel_sweep
    sweeper = TtlSweeper(pool, 5.0, sleep_fn=_SleepRecorder(stop_after=None))
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()
