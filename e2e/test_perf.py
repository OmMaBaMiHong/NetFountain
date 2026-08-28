"""PERF-01~03 压测用例（真实链路为主）。

- PERF-01 百并发 acquire（直接打 level2 与经 proxy 各一轮）→ 延迟 p50/p99、无重复分配；
- PERF-02 代理层百并发透传（baidu/gongshang 混合）→ 正确率 100%、无串扰、吞吐；
- PERF-03 真实 level1 持续拉取 5 分钟 → 池 ≤500、内存稳定。
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from client import (
    api_get,
    api_post,
    level1_status,
    percentile,
    pool_free,
    pool_free_gte,
    pool_stats,
    wait_until,
)
from processes import BASE


@pytest.mark.perf
async def test_perf_01_concurrent_acquire(svc):
    """百并发 acquire：无重复分配、失败为 EMPTY_POOL、输出 p50/p99。"""
    gs = BASE["gongshang"]
    report = {}
    for label, base in (
        ("direct", f"{gs}/api/v1"),
        ("proxy", f"{BASE['proxy']}/api/v1/gongshang"),
    ):
        await wait_until(lambda: pool_free_gte(gs, 1), 600, 3, f"{label} gongshang free")
        free0 = await pool_free(gs)
        async with httpx.AsyncClient(timeout=30) as c:
            async def _one():
                t0 = time.perf_counter()
                st, body = await api_post(c, f"{base}/ips/acquire")
                return (time.perf_counter() - t0) * 1000.0, st, body

            results = await asyncio.gather(*(_one() for _ in range(100)))
        lats = [r[0] for r in results]
        ok_ids = [r[2]["data"]["id"] for r in results if r[2]["code"] == 0]
        ok_urls = [r[2]["data"]["proxy_url"] for r in results if r[2]["code"] == 0]
        failures = [r[2] for r in results if r[2]["code"] != 0]
        assert len(ok_ids) == len(set(ok_ids)), f"{label}: 重复分配 id"
        assert len(ok_urls) == len(set(ok_urls)), f"{label}: 重复分配 proxy_url"
        assert all(b["code"] == 40402 for b in failures), f"{label}: 非空池失败码 {failures}"
        assert len(ok_ids) >= min(100, free0), f"{label}: 成功数 < min(100, free0)"
        assert len(ok_ids) <= 100
        report[label] = {
            "free0": free0,
            "success": len(ok_ids),
            "empty": len(failures),
            "p50_ms": round(percentile(lats, 50), 1),
            "p99_ms": round(percentile(lats, 99), 1),
        }
        # 清理：释放本轮获取的全部
        async with httpx.AsyncClient(timeout=30) as c2:
            for rec_id in ok_ids:
                await api_post(c2, f"{base}/ips/{rec_id}/release")
    print(f"PERF-01 result: {report}")


@pytest.mark.perf
async def test_perf_02_proxy_concurrency(svc):
    """代理层百并发透传（baidu/gongshang 混合）：正确率 100%、无串扰、记录吞吐。"""
    baidu, gongshang = BASE["baidu"], BASE["gongshang"]
    b0 = (await level1_status(baidu) or {}).get("api_call_count", 0)
    g0 = (await level1_status(gongshang) or {}).get("api_call_count", 0)
    N = 100
    urls = [f"{BASE['proxy']}/api/v1/{site}/count" for site in ("baidu", "gongshang")] * (N // 2)
    async with httpx.AsyncClient(timeout=30) as c:
        t0 = time.perf_counter()
        results = await asyncio.gather(*(api_get(c, u) for u in urls))
        total_ms = (time.perf_counter() - t0) * 1000.0
    bad = [r for r in results if r[0] != 200 or r[1].get("code") != 0]
    assert not bad, f"透传失败 {len(bad)} 个: {bad[:3]}"
    # api_call_count 增量 = 请求数 + 1（最后读取 status 的探测请求自身也会 +1）；
    # 若发生串扰/丢失，各站点增量会偏离预期。
    b1 = (await level1_status(baidu) or {}).get("api_call_count", 0)
    g1 = (await level1_status(gongshang) or {}).get("api_call_count", 0)
    assert b1 - b0 == N // 2 + 1, f"baidu 收到 {b1-b0} 次（应 {N//2+1}），存在串扰/丢失"
    assert g1 - g0 == N // 2 + 1, f"gongshang 收到 {g1-g0} 次（应 {N//2+1}），存在串扰/丢失"
    baidu_bodies = [r[1] for r, u in zip(results, urls) if "baidu" in u]
    gs_bodies = [r[1] for r, u in zip(results, urls) if "gongshang" in u]
    assert all(b == baidu_bodies[0] for b in baidu_bodies), "baidu 响应不一致"
    assert all(b == gs_bodies[0] for b in gs_bodies), "gongshang 响应不一致"
    print(
        f"PERF-02 result: {N} reqs, {total_ms:.1f}ms, "
        f"{N/(total_ms/1000.0):.1f} req/s, correct=100%, no-crosstalk"
    )


@pytest.mark.perf
async def test_perf_03_level1_sustained(svc):
    """真实 level1 持续拉取 5 分钟：池 ≤500、内存稳定、无异常增长。"""
    import psutil  # noqa: PLC0415

    l1 = BASE["level1"]
    proc = svc.procs["level1"].proc
    p = psutil.Process(proc.pid)
    samples = []
    t0 = time.time()
    while time.time() - t0 < 300:
        st = await level1_status(l1)
        samples.append((time.time() - t0, st["pool_size"], st["total_pulled"], p.memory_info().rss))
        await asyncio.sleep(30)
    sizes = [s[1] for s in samples]
    rss = [s[3] for s in samples]
    assert max(sizes) <= 500, f"池超上限: {max(sizes)}"
    assert rss[-1] <= rss[0] * 1.5 + 20 * 1024 * 1024, "内存异常增长"
    print(
        f"PERF-03 result: samples={len(samples)} pool_max={max(sizes)} "
        f"rss_first={rss[0]/1024/1024:.1f}MB rss_last={rss[-1]/1024/1024:.1f}MB "
        f"pulled_growth={samples[-1][2]-samples[0][2]}"
    )