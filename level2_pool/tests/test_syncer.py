"""syncer.py 测试：全量/增量/空响应重置/异常隔离/水位线推进。

覆盖测试计划书 L2-SYNC-001 ~ 008。
"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from app.pool import Level2Pool, ServiceStats
from app.syncer import Level1SyncClient, SyncTask

BASE = "http://level1.test"

_OK = {"code": 0, "msg": "ok"}


def _payload(*records: dict) -> dict:
    return {**_OK, "data": list(records)}


def _ip_dict(idx: int, id_: int | None = None, protocol: str = "http") -> dict:
    pid = id_ if id_ is not None else idx
    return {
        "id": pid,
        "ip": f"1.2.3.{idx}",
        "port": 8080 + idx,
        "protocol": protocol,
        "proxy_url": f"{protocol}://1.2.3.{idx}:{8080 + idx}",
        "region": "CN",
        "ttl": 120.0,
        "created_at": 1000.0,
    }


async def _make_task(mock_session, *, site_fn=None, interval=3.0, sleep_fn=None, stats=None):
    session, _ = mock_session
    client = Level1SyncClient(BASE, session, timeout=2.0)
    pool = Level2Pool()
    if stats is None:
        stats = ServiceStats()
    from app.tester import Tester

    tester = Tester(
        target_url="http://www.baidu.com",
        threshold_ms=2000,
        connect_timeout=1.0,
        concurrency=5,
        site_fn=site_fn or (lambda rec: (True, 100.0)),
    )
    task = SyncTask(client, tester, pool, stats, interval=interval, sleep_fn=sleep_fn)
    return client, pool, stats, tester, task


# ---------------------------------------------------------------------------
# Level1SyncClient 契约
# ---------------------------------------------------------------------------


async def test_client_fetch_all(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2)))
    client = Level1SyncClient(BASE, session)
    records = await client.fetch_all()
    assert [r.id for r in records] == [1, 2]
    assert records[0].proxy_url == "http://1.2.3.1:8081"
    assert records[0].protocol.value == "http"
    assert records[0].region == "CN"
    assert records[0].ttl == 120.0


async def test_client_fetch_all_empty(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload())
    client = Level1SyncClient(BASE, session)
    assert await client.fetch_all() == []


async def test_client_fetch_after(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips/after/5", payload=_payload(_ip_dict(6, id_=6)))
    client = Level1SyncClient(BASE, session)
    records = await client.fetch_after(5)
    assert [r.id for r in records] == [6]


async def test_client_fetch_after_empty(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips/after/5", payload=_payload())
    client = Level1SyncClient(BASE, session)
    assert await client.fetch_after(5) == []


async def test_client_fetch_after_proxy_url_rebuilt(mock_session):
    session, m = mock_session
    item = _ip_dict(1)
    item.pop("proxy_url")
    m.get(f"{BASE}/api/v1/ips", payload=_payload(item))
    client = Level1SyncClient(BASE, session)
    records = await client.fetch_all()
    assert records[0].proxy_url == "http://1.2.3.1:8081"


async def test_client_non_200_raises(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", status=500)
    client = Level1SyncClient(BASE, session)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.fetch_all()


async def test_client_non_object_payload_raises(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=[1, 2, 3])
    client = Level1SyncClient(BASE, session)
    with pytest.raises(ValueError):
        await client.fetch_all()


async def test_client_missing_data_key_returns_empty(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload={"code": 0, "msg": "ok"})
    client = Level1SyncClient(BASE, session)
    assert await client.fetch_all() == []


async def test_client_data_not_list_raises(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload={"code": 0, "msg": "ok", "data": "not-a-list"})
    client = Level1SyncClient(BASE, session)
    with pytest.raises(ValueError):
        await client.fetch_all()


# ---------------------------------------------------------------------------
# SyncTask
# ---------------------------------------------------------------------------


async def test_first_sync_full_and_watermark(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2), _ip_dict(3)))
    _, pool, stats, _, task = await _make_task(mock_session)
    await task._sync_once()
    assert task.last_synced_id == 3
    assert stats.last_synced_id == 3
    assert stats.total_pulled == 3
    assert stats.total_entered == 3
    assert len(pool.all()) == 3


async def test_incremental_advances_watermark(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2)))
    m.get(f"{BASE}/api/v1/ips/after/2", payload=_payload(_ip_dict(3, id_=3), _ip_dict(4, id_=4)))
    _, pool, stats, _, task = await _make_task(mock_session)
    await task._sync_once()
    assert task.last_synced_id == 2
    await task._sync_once()
    assert task.last_synced_id == 4
    assert stats.total_pulled == 4
    assert len(pool.all()) == 4


async def test_empty_response_triggers_full_repull(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2), _ip_dict(3)))
    # 一级池重启：after 返回空，全量返回新的低 id 记录（含新身份）
    m.get(f"{BASE}/api/v1/ips/after/3", payload=_payload())
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1, id_=1), _ip_dict(2, id_=2), _ip_dict(4, id_=3)))
    _, pool, _, _, task = await _make_task(mock_session)
    await task._sync_once()
    assert task.last_synced_id == 3
    await task._sync_once()
    # 空响应 → 全量重拉 → 新记录（1.2.3.4）入池，水位线重置为 3
    assert task.last_synced_id == 3
    assert [r.ip for r in pool.all()] == ["1.2.3.1", "1.2.3.2", "1.2.3.3", "1.2.3.4"]


async def test_empty_response_resets_watermark(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2), _ip_dict(3)))
    # after(3) 空 → 全量只剩 id 1、2（模拟 id 空间归零）
    m.get(f"{BASE}/api/v1/ips/after/3", payload=_payload())
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(3, id_=1), _ip_dict(4, id_=2)))
    _, pool, _, _, task = await _make_task(mock_session)
    await task._sync_once()
    assert task.last_synced_id == 3
    await task._sync_once()
    assert task.last_synced_id == 2
    assert len(pool.all()) == 4  # 旧 3 条保留 + 新增 1 条（1.2.3.4）


async def test_reset_does_not_remove_existing_records(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2)))
    m.get(f"{BASE}/api/v1/ips/after/2", payload=_payload())
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(3, id_=1), _ip_dict(4, id_=2)))
    _, pool, _, _, task = await _make_task(mock_session)
    await task._sync_once()
    old_ids = {r.id for r in pool.all()}
    await task._sync_once()
    assert pool.stats().total == 4
    assert {r.id for r in pool.all()} >= old_ids


async def test_reset_keeps_leased_records(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2)))
    m.get(f"{BASE}/api/v1/ips/after/2", payload=_payload())
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(3, id_=1), _ip_dict(4, id_=2)))
    _, pool, _, _, task = await _make_task(mock_session)
    await task._sync_once()
    leased = await pool.acquire()
    assert leased is not None
    await task._sync_once()
    assert leased.leased is True
    assert leased in pool.all()


async def test_site_filter_threshold_applies(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2), _ip_dict(3)))

    def _site(rec):
        lat = {"1.2.3.1": 100.0, "1.2.3.2": 2500.0, "1.2.3.3": 1999.0}[rec.ip]
        return True, lat

    _, pool, stats, _, task = await _make_task(mock_session, site_fn=_site)
    await task._sync_once()
    assert [r.ip for r in pool.all()] == ["1.2.3.1", "1.2.3.3"]
    assert stats.total_pulled == 3
    assert stats.total_entered == 2


async def test_first_full_empty_keeps_none(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload())
    _, pool, stats, _, task = await _make_task(mock_session)
    await task._sync_once()
    assert task.last_synced_id is None
    assert stats.last_synced_id is None
    assert pool.stats().total == 0


async def test_sync_error_does_not_affect_pool(mock_session):
    """一级池不可达：现有记录不受影响，循环继续重试。"""
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1), _ip_dict(2)))
    m.get(f"{BASE}/api/v1/ips/after/2", status=500)
    m.get(f"{BASE}/api/v1/ips/after/2", payload=_payload(_ip_dict(3, id_=3)))

    sleeps = []

    async def _sleep(duration):
        sleeps.append(duration)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    _, pool, stats, _, task = await _make_task(
        mock_session, interval=0.01, sleep_fn=_sleep
    )
    await task._sync_once()  # tick1 健康，入池 2 条
    assert len(pool.all()) == 2

    with pytest.raises(asyncio.CancelledError):
        await task.run()
    # tick2 失败（不影响池），tick3 恢复后新增
    assert len(pool.all()) == 3
    assert task.last_synced_id == 3
    assert stats.total_pulled == 3


async def test_sync_task_run_interval(mock_session):
    """同步周期 ≈ sync_interval。"""
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1)))
    m.get(f"{BASE}/api/v1/ips/after/1", payload=_payload(_ip_dict(1, id_=1)))

    sleeps = []

    async def _sleep(duration):
        sleeps.append(duration)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    _, pool, stats, _, task = await _make_task(
        mock_session, interval=1.5, sleep_fn=_sleep
    )
    with pytest.raises(asyncio.CancelledError):
        await task.run()
    assert sleeps == [1.5, 1.5]
    assert stats.total_pulled == 2
    assert len(pool.all()) == 1  # 同一身份去重


async def test_sync_task_cancellation(mock_session):
    session, m = mock_session
    m.get(f"{BASE}/api/v1/ips", payload=_payload(_ip_dict(1)))
    _, pool, _, _, task = await _make_task(mock_session, interval=1.0)

    async def _never(duration):
        await asyncio.sleep(3600)

    task._sleep = _never
    t = asyncio.create_task(task.run())
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert len(pool.all()) == 1


async def test_sync_task_cancelled_during_tick(mock_session):
    """tick 内取消 → CancelledError 穿透（run 的 CancelledError 分支）。"""
    _, pool, _, _, task = await _make_task(mock_session, interval=1.0)

    async def _cancel_tick():
        raise asyncio.CancelledError()

    task._sync_once = _cancel_tick
    with pytest.raises(asyncio.CancelledError):
        await task.run()