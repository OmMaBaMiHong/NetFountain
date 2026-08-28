"""pool.py 测试：upsert 去重 / 本地 id / 租赁语义 / 原子分配 / 计数 / TTL 淘汰。

覆盖测试计划书 L2-POOL-001 ~ 013。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from ip_pool_common.models import Level2Record, Protocol


def _l2(make_l2, make_ip, idx, protocol=Protocol.HTTP, latency=10.0, **kwargs) -> Level2Record:
    return make_l2(make_ip(idx, protocol=protocol), latency=latency, **kwargs)


async def test_upsert_dedup_by_proxy_url(pool, make_l2, make_ip):
    rec1 = _l2(make_l2, make_ip, 1, latency=10.0)
    returned1 = await pool.upsert(rec1)
    assert pool.stats().total == 1

    rec2 = _l2(make_l2, make_ip, 1, latency=2500.0, created_at=2000.0)
    returned2 = await pool.upsert(rec2)
    assert pool.stats().total == 1
    assert returned2.id == returned1.id
    assert returned2.latency_ms == 2500.0
    assert returned2.created_at == 2000.0


async def test_upsert_keeps_leased_state(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    acquired = await pool.acquire()
    assert acquired is not None and acquired.leased

    refreshed = await pool.upsert(_l2(make_l2, make_ip, 1, latency=99.0))
    assert refreshed.id == acquired.id
    assert refreshed.leased is True
    assert refreshed.leased_at == acquired.leased_at


async def test_local_id_monotonic_no_reuse(pool, make_l2, make_ip):
    ids = []
    for i in range(1, 5):
        rec = await pool.upsert(_l2(make_l2, make_ip, i))
        ids.append(rec.id)
    assert ids == [0, 1, 2, 3]

    removed = await pool.remove(1)
    assert removed is True
    new = await pool.upsert(_l2(make_l2, make_ip, 9))
    assert new.id == 4
    assert new.id not in ids


async def test_acquire_newest_first(pool, make_l2, make_ip):
    for i in range(1, 4):
        await pool.upsert(_l2(make_l2, make_ip, i))
    r1 = await pool.acquire()
    r2 = await pool.acquire()
    r3 = await pool.acquire()
    r4 = await pool.acquire()
    assert [r1.ip, r2.ip, r3.ip] == ["10.0.0.3", "10.0.0.2", "10.0.0.1"]
    assert r4 is None
    assert all(r.leased for r in (r1, r2, r3))


async def test_acquire_skips_leased(pool, make_l2, make_ip):
    for i in range(1, 4):
        await pool.upsert(_l2(make_l2, make_ip, i))
    newest = await pool.acquire()
    assert newest.ip == "10.0.0.3"
    second = await pool.acquire()
    assert second.ip == "10.0.0.2"
    third = await pool.acquire()
    assert third.ip == "10.0.0.1"
    assert await pool.acquire() is None


async def test_lease_has_no_expiry(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    rec = await pool.acquire()
    assert rec is not None and rec.leased is True
    leased_at = rec.leased_at
    await asyncio.sleep(0.02)
    assert await pool.acquire() is None
    assert rec.leased is True
    assert rec.leased_at == leased_at


async def test_acquire_atomic_100_concurrent(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    results = await asyncio.gather(*(pool.acquire() for _ in range(100)))
    successes = [r for r in results if r is not None]
    assert len(successes) == 1
    assert successes[0].leased is True
    assert all(r is None for r in results[1:])
    assert pool.stats().leased_total == 1


async def test_release(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    rec = await pool.acquire()
    assert rec is not None
    released = await pool.release(rec.id)
    assert released is True
    assert rec.leased is False
    assert rec.leased_at is None
    again = await pool.acquire()
    assert again is not None and again.id == rec.id


async def test_release_of_free_record_ok(pool, make_l2, make_ip):
    rec = await pool.upsert(_l2(make_l2, make_ip, 1))
    assert await pool.release(rec.id) is True
    assert rec.leased is False


async def test_remove(pool, make_l2, make_ip):
    rec = await pool.upsert(_l2(make_l2, make_ip, 1))
    assert await pool.remove(rec.id) is True
    assert pool.stats().total == 0
    assert await pool.acquire() is None


async def test_remove_leased(pool, make_l2, make_ip):
    rec = await pool.upsert(_l2(make_l2, make_ip, 1))
    await pool.acquire()
    assert rec.leased is True
    assert await pool.remove(rec.id) is True
    assert pool.stats().total == 0


async def test_release_all(pool, make_l2, make_ip):
    for i in range(1, 4):
        await pool.upsert(_l2(make_l2, make_ip, i))
    acquired = []
    for _ in range(3):
        rec = await pool.acquire()
        assert rec is not None
        acquired.append(rec)
    assert pool.stats().leased_total == 3
    count = await pool.release_all()
    assert count == 3
    assert pool.stats().leased_total == 0
    assert all(rec.leased is False for rec in pool.all())


async def test_release_all_none_leased(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    assert await pool.release_all() == 0


async def test_invalid_id_returns_false(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    assert await pool.release(999) is False
    assert await pool.remove(999) is False
    assert pool.stats().total == 1


async def test_stats_breakdown(mixed_pool):
    stats = mixed_pool.stats()
    assert stats.total == 5
    assert stats.by_proto == {
        Protocol.HTTP: 2,
        Protocol.HTTPS: 1,
        Protocol.SOCKS4: 1,
        Protocol.SOCKS5: 1,
    }
    assert stats.leased_total == 2
    assert stats.leased_by_proto == {Protocol.HTTPS: 1, Protocol.SOCKS5: 1}
    assert stats.free_total == 3
    assert stats.free_by_proto == {
        Protocol.HTTP: 2,
        Protocol.SOCKS4: 1,
    }


async def test_stats_empty_pool(pool):
    stats = pool.stats()
    assert stats.total == 0
    assert stats.by_proto == {}
    assert stats.leased_total == 0
    assert stats.free_total == 0


async def test_all_does_not_change_state(mixed_pool):
    before = mixed_pool.stats()
    snapshot1 = mixed_pool.all()
    snapshot2 = mixed_pool.all()
    after = mixed_pool.stats()
    assert len(snapshot1) == len(snapshot2) == before.total
    assert [r.leased for r in snapshot2] == [r.leased for r in snapshot1]
    assert after.total == before.total
    assert after.leased_total == before.leased_total
    assert all(
        r1.leased == r2.leased for r1, r2 in zip(snapshot1, snapshot2)
    )


async def test_all_preserves_order(pool, make_l2, make_ip):
    for i in range(1, 4):
        await pool.upsert(_l2(make_l2, make_ip, i))
    assert [r.ip for r in pool.all()] == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


async def test_sweep_ttl_removes_only_expired(pool, make_l2, make_ip):
    now = time.time()
    await pool.upsert(
        make_l2(make_ip(1, ttl=5.0), created_at=now - 10.0)   # 过期
    )
    await pool.upsert(
        make_l2(make_ip(2, ttl=1000.0), created_at=now - 10.0)  # 未过期
    )
    await pool.upsert(
        make_l2(make_ip(3, ttl=None), created_at=now - 10.0)   # 永不
    )
    removed = await pool.sweep_ttl(now)
    assert removed == 1
    remaining = pool.all()
    assert [r.ip for r in remaining] == ["10.0.0.2", "10.0.0.3"]


async def test_sweep_ttl_removes_leased_expired(pool, make_l2, make_ip):
    now = time.time()
    await pool.upsert(make_l2(make_ip(1, ttl=5.0), created_at=now - 10.0))
    rec = await pool.acquire()
    assert rec is not None and rec.leased
    assert await pool.sweep_ttl(now) == 1
    assert pool.stats().total == 0


async def test_sweep_ttl_nothing_expired(pool, make_l2, make_ip):
    now = time.time()
    await pool.upsert(make_l2(make_ip(1, ttl=1000.0), created_at=now))
    assert await pool.sweep_ttl(now) == 0
    assert pool.stats().total == 1


async def test_sweep_ttl_after_upsert_refresh(pool, make_l2, make_ip):
    """upsert 刷新 created_at 后，TTL 基准随之刷新，过期判定不误删。"""
    now = time.time()
    await pool.upsert(make_l2(make_ip(1, ttl=5.0), created_at=now - 10.0))
    await pool.sweep_ttl(now - 1.0)  # 恰好仍过期
    refreshed = await pool.upsert(
        make_l2(make_ip(1, ttl=5.0), created_at=now)
    )
    assert refreshed.created_at == now
    assert await pool.sweep_ttl(now) == 0
    assert pool.stats().total == 1


async def test_acquire_release_roundtrip_stats(pool, make_l2, make_ip):
    await pool.upsert(_l2(make_l2, make_ip, 1))
    rec = await pool.acquire()
    assert pool.stats().leased_total == 1
    await pool.release(rec.id)
    assert pool.stats().leased_total == 0
    assert pool.stats().free_total == 1


async def test_remove_nonexistent_after_remove(pool, make_l2, make_ip):
    rec = await pool.upsert(_l2(make_l2, make_ip, 1))
    assert await pool.remove(rec.id) is True
    assert await pool.remove(rec.id) is False
    assert await pool.release(rec.id) is False


async def test_next_id_never_reused_after_remove(pool, make_l2, make_ip):
    first = await pool.upsert(_l2(make_l2, make_ip, 1))
    await pool.upsert(_l2(make_l2, make_ip, 2))
    await pool.remove(first.id)
    assert pool.next_id == 2
    third = await pool.upsert(_l2(make_l2, make_ip, 3))
    assert third.id == 2