"""E2E-01~09 端到端用例。

模式标注：
- 真实：E2E-01/02/04/07/08 —— 使用真实 91HTTP 供应商链路（宽松超时 + 重试）；
- 真实为主/mock 辅助：E2E-03 —— 目标不可达（不可达地址），依赖真实链路；
- mock 辅助：E2E-05/06/09 —— 确定性故障注入与 TTL，使用 e2e/mock_provider + mock_site。

场景行（真实为准）：
  E2E-01 全链路闭环、E2E-02 多站点隔离、E2E-04 一级池重启恢复、
  E2E-07 配置热更新、E2E-08 限频。
"""
from __future__ import annotations

import asyncio
import os
import time

import httpx
import pytest
import yaml

from client import (
    api_get,
    api_post,
    level1_pulled_gte,
    level1_size_gte,
    level1_status,
    level2_status,
    level2_synced_lt,
    mock_admin,
    pool_ips,
    pool_stats,
    pool_total,
    pool_total_eq,
    pool_total_gte,
    proxy_egress,
    reset_mock_provider,
    wait_until,
)
from processes import (
    BASE,
    CONFIG_DIR,
    P_LEVEL1,
    P_MOCK_LEVEL1_TTL,
    P_TMP_EMPTY,
    P_TMP_MOCK_L2,
    P_TMP_RESTART,
    P_TMP_TTL,
    Services,
)

GONGSHANG_TARGET = "https://gongshang.mingluji.com/"


# ---------------------------------------------------------------------------
# E2E-01 全链路（真实）
# ---------------------------------------------------------------------------


async def test_e2e_01_full_chain(svc):
    """经 proxy 对 gongshang acquire → IP → 出口请求 → release → 可再次获取。"""
    gs = BASE["gongshang"]
    gs_proxy = f"{BASE['proxy']}/api/v1/gongshang"
    await wait_until(
        lambda: pool_total_gte(gs, 1), timeout=600, interval=3, desc="gongshang pool fill"
    )
    async with httpx.AsyncClient(timeout=20) as c:
        rec = None
        for _ in range(6):
            st, body = await api_post(c, f"{gs_proxy}/ips/acquire")
            assert st == 200
            if body["code"] == 0:
                rec = body["data"]
                break
            await asyncio.sleep(3)
        assert rec is not None, "acquire 重试 6 次仍未拿到 IP（真实池可用率过低）"
        proxy_url = rec["proxy_url"]
        ok, status = False, None
        for _ in range(4):
            ok, status = await proxy_egress(proxy_url, GONGSHANG_TARGET, timeout=15)
            if ok:
                break
            await asyncio.sleep(2)
        assert ok, f"出口请求失败：{proxy_url} 最后状态={status}（连接/代理错误为重试项）"
        st, body = await api_post(c, f"{gs_proxy}/ips/{rec['id']}/release")
        assert st == 200 and body["code"] == 0, "release 失败"
        st, body = await api_post(c, f"{gs_proxy}/ips/acquire")
        assert st == 200 and body["code"] == 0, "release 后应可再次获取"
        await api_post(c, f"{gs_proxy}/ips/{body['data']['id']}/release")


# ---------------------------------------------------------------------------
# E2E-02 多站点隔离（真实）
# ---------------------------------------------------------------------------


async def test_e2e_02_site_isolation(svc):
    """baidu 与 gongshang 独立池：acquire 互不影响，count 各自正确。"""
    baidu, gongshang = BASE["baidu"], BASE["gongshang"]
    await wait_until(lambda: pool_total_gte(baidu, 1), 600, 3, "baidu pool fill")
    await wait_until(lambda: pool_total_gte(gongshang, 1), 600, 3, "gongshang pool fill")
    async with httpx.AsyncClient(timeout=20) as c:
        b0 = await pool_stats(baidu)
        g0 = await pool_stats(gongshang)
        st, body = await api_post(c, f"{BASE['proxy']}/api/v1/baidu/ips/acquire")
        assert st == 200 and body["code"] == 0
        rec = body["data"]
        b1 = await pool_stats(baidu)
        g1 = await pool_stats(gongshang)
        assert b1["leased_total"] == b0["leased_total"] + 1, "baidu 租赁未 +1"
        assert g1["leased_total"] == g0["leased_total"], "gongshang 受 baidu acquire 影响"
        assert str(rec["id"]) in {str(i["id"]) for i in await pool_ips(baidu)}
        st, body = await api_post(c, f"{BASE['proxy']}/api/v1/baidu/ips/{rec['id']}/release")
        assert st == 200 and body["code"] == 0
        b2 = await pool_stats(baidu)
        assert b2["leased_total"] == b0["leased_total"], "release 后 baidu 租赁未还原"
        st, body = await api_post(c, f"{BASE['proxy']}/api/v1/gongshang/ips/acquire")
        assert st == 200 and body["code"] == 0
        rec2 = body["data"]
        b3 = await pool_stats(baidu)
        g3 = await pool_stats(gongshang)
        assert b3["leased_total"] == b0["leased_total"], "baidu 受 gongshang acquire 影响"
        assert g3["leased_total"] == g0["leased_total"] + 1, "gongshang 租赁未 +1"
        assert str(rec2["id"]) in {str(i["id"]) for i in await pool_ips(gongshang)}
        await api_post(c, f"{BASE['proxy']}/api/v1/gongshang/ips/{rec2['id']}/release")


# ---------------------------------------------------------------------------
# E2E-03 空池（真实为主）
# ---------------------------------------------------------------------------


async def test_e2e_03_empty_pool(svc):
    """临时 level2 目标不可达（http://10.255.255.1:9）→ acquire 返回 EMPTY_POOL(40402)。"""
    s = Services()
    s.start_uvicorn("tmp_empty_l2", "level2", "level2_empty_pool.yaml", "LEVEL2_", P_TMP_EMPTY)
    try:
        tmp = f"http://127.0.0.1:{P_TMP_EMPTY}"
        s.wait_http(f"{tmp}/api/v1/status", timeout=90, desc="tmp empty level2")
        await asyncio.sleep(5)  # 至少几个同步 tick（site_test 对不可达目标必败）
        async with httpx.AsyncClient(timeout=20) as c:
            st, body = await api_post(c, f"{tmp}/api/v1/ips/acquire")
            assert st == 200, "空池 acquire 不应 500"
            assert body["code"] == 40402, f"应返回 EMPTY_POOL(40402)，实际 {body}"
            st, body = await api_get(c, f"{tmp}/api/v1/ips")
            assert st == 200 and body["code"] == 0 and body["data"] == [], "空池 /ips 应为空"
    finally:
        s.stop_all()


# ---------------------------------------------------------------------------
# E2E-04 一级池重启恢复（真实）
# ---------------------------------------------------------------------------


async def test_e2e_04_level1_restart(svc):
    """重启真实 level1（id 归零）→ 二级池空响应 → 全量重拉 + 水位线重置，现存记录不被清除。"""
    s = Services()
    s.start_uvicorn("tmp_restart_l2", "level2", "level2_restart.yaml", "LEVEL2_", P_TMP_RESTART)
    try:
        tmp = f"http://127.0.0.1:{P_TMP_RESTART}"
        s.wait_http(f"{tmp}/api/v1/status", timeout=90, desc="tmp restart level2")
        await wait_until(lambda: pool_total_gte(tmp, 1), 600, 3, "restart l2 pool fill")
        before = {int(r["id"]) for r in await pool_ips(tmp)}
        assert before, "重启前临时二级池应已有记录"
        old_synced = (await level2_status(tmp)).get("last_synced_id")
        assert old_synced is not None

        # 重启 session 真实 level1（id 归零）
        svc.stop("level1")
        svc.start_uvicorn("level1", "level1", "level1.yaml", "LEVEL1_", P_LEVEL1)
        svc.wait_http(f"{BASE['level1']}/api/v1/status", timeout=120, desc="level1 restart")

        # 空响应 → 全量重拉 → 水位线重置：last_synced_id 进入新 id 空间（< 旧值）
        await wait_until(lambda: level2_synced_lt(tmp, old_synced), 600, 3, "level2 watermark reset")
        after = {int(r["id"]) for r in await pool_ips(tmp)}
        assert before.issubset(after), "重启后现存记录被清除（应保留）"
        l1_now = await level1_status(BASE["level1"])
        l2_now = await level2_status(tmp)
        assert l2_now["last_synced_id"] <= l1_now["next_id"], "水位线未重置到新 id 空间"
    finally:
        s.stop_all()


# ---------------------------------------------------------------------------
# E2E-05 复验闭环（mock 辅助）
# ---------------------------------------------------------------------------


async def test_e2e_05_revalidate_cleanup(svc, mock_env):
    """mock_provider 供 IP 的临时 level2：停掉部分代理端口 → revalidate 后失效 IP 被删除（含租赁中）。"""
    mp = BASE["mock_provider"]
    await reset_mock_provider(mp)
    s = Services()
    s.start_uvicorn("tmp_mock_l2", "level2", "level2_mock_site.yaml", "LEVEL2_", P_TMP_MOCK_L2)
    try:
        tmp = f"http://127.0.0.1:{P_TMP_MOCK_L2}"
        s.wait_http(f"{tmp}/api/v1/status", timeout=90, desc="tmp mock level2")
        await wait_until(lambda: pool_total_gte(tmp, 10), 120, 1, "mock level2 pool fill")

        async with httpx.AsyncClient(timeout=15) as c:
            acquired = []
            for _ in range(60):
                st, body = await api_post(c, f"{tmp}/api/v1/ips/acquire")
                if body["code"] == 0:
                    acquired.append(body["data"])
                else:
                    break
        assert len(acquired) >= 10, "mock 池应能租赁 ≥10 个"
        assert all(a["leased"] is True for a in await pool_ips(tmp)), "应全部处于租赁中"

        kill = [int(a["port"]) for a in acquired[:5]]
        await mock_admin(mp, "/admin/down", {"ports": kill})

        # 等 > revalidate_interval(10s) + 余量
        await asyncio.sleep(16)
        remain = {int(r["port"]) for r in await pool_ips(tmp)}
        for p in kill:
            assert p not in remain, f"失效端口 {p} 未被复验清除（含租赁中应删除）"

        # 恢复端口 → 复验/同步后重新入池
        await mock_admin(mp, "/admin/up", {"ports": kill})
        await wait_until(lambda: _ports_all_present(tmp, kill), 90, 2, "ports restored")
    finally:
        await reset_mock_provider(mp)
        s.stop_all()


async def _ports_all_present(base: str, ports: list[int]) -> bool:
    present = {int(r["port"]) for r in await pool_ips(base)}
    return all(p in present for p in ports)


# ---------------------------------------------------------------------------
# E2E-06 供应商故障恢复（mock 辅助）
# ---------------------------------------------------------------------------


async def test_e2e_06_provider_fault_recovery(svc, mock_env):
    """mock_provider 全部端口故障 + API 500 → 临时 level1 拉取失败不崩溃 → 恢复后自动续拉。"""
    mp = BASE["mock_provider"]
    ml1 = BASE["mock_level1"]
    await reset_mock_provider(mp)
    await wait_until(lambda: level1_size_gte(ml1, 3), 120, 1, "mock level1 fill")
    st1 = await level1_status(ml1)
    pulled1 = st1["total_pulled"]

    await mock_admin(mp, "/admin/fail")
    await mock_admin(mp, "/admin/down", {})
    await asyncio.sleep(6)
    during = await level1_status(ml1)  # 不崩溃：status 仍可访问
    assert during["uptime"] > st1["uptime"], "故障期间 uptime 应继续增长（进程未崩）"
    assert during["total_pulled"] >= pulled1, "total_pulled 不应倒退"

    await mock_admin(mp, "/admin/recover")
    await mock_admin(mp, "/admin/up", {})
    await wait_until(lambda: level1_pulled_gte(ml1, pulled1 + 5), 120, 2, "mock level1 resume")
    await wait_until(lambda: level1_size_gte(ml1, 3), 120, 2, "mock level1 refill")

    real_alive = await level1_status(BASE["level1"])
    assert real_alive.get("pool_size", -1) >= 0, "真实供应商链路不受 mock 故障影响"


# ---------------------------------------------------------------------------
# E2E-07 配置热更新（真实）
# ---------------------------------------------------------------------------


async def test_e2e_07_hot_reload(svc):
    """真实链路新增 site_c 路由 → reload_interval(10s) 后无需重启即可访问。"""
    routes_path = os.path.join(CONFIG_DIR, "proxy_routes.yaml")
    with open(routes_path, "r", encoding="utf-8") as f:
        original = f.read()
    proxy = BASE["proxy"]
    async with httpx.AsyncClient(timeout=20) as c:
        st, body = await api_get(c, f"{proxy}/api/v1/health")
        assert {s["name"] for s in body["data"]["sites"]} == {"baidu", "gongshang"}
        st, body = await api_get(c, f"{proxy}/api/v1/site_c/count")
        assert body["code"] == 40400, "site_c 未配置应返回 40400"
    try:
        data = yaml.safe_load(original)
        data["sites"].append(
            {
                "name": "site_c",
                "base_url": "http://127.0.0.1:8001",
                "target_url": "http://www.baidu.com",
            }
        )
        with open(routes_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        await asyncio.sleep(14)  # > reload_interval(10s)
        async with httpx.AsyncClient(timeout=20) as c:
            st, body = await api_get(c, f"{proxy}/api/v1/site_c/count")
            assert st == 200 and body["code"] == 0, f"热更新后 site_c 不可访问: {body}"
            st, b = await api_get(c, f"{proxy}/api/v1/baidu/count")
            assert b["code"] == 0 and b["data"] == body["data"], "site_c 应路由到 baidu level2"
            st, h = await api_get(c, f"{proxy}/api/v1/health")
            assert "site_c" in {s["name"] for s in h["data"]["sites"]}
    finally:
        with open(routes_path, "w", encoding="utf-8") as f:
            f.write(original)
        await asyncio.sleep(12)  # 等代理重载还原，保证可重复执行


# ---------------------------------------------------------------------------
# E2E-08 限频（真实）
# ---------------------------------------------------------------------------


async def test_e2e_08_rate_limit(svc):
    """观察真实 level1 拉取统计：拉取间隔 ≥1s（total_pulled 增长速率有界）。"""
    l1 = BASE["level1"]
    st1 = await level1_status(l1)
    pulled1 = st1["total_pulled"]
    pool1 = st1["pool_size"]
    t0 = time.time()
    await asyncio.sleep(12)
    st2 = await level1_status(l1)
    elapsed = time.time() - t0
    delta = st2["total_pulled"] - pulled1
    max_allowed = (elapsed + 1.5) * 10  # pull_count=10，tick 间隔 ≥1s
    assert delta <= max_allowed, f"疑似拉取间隔 <1s: delta={delta} in {elapsed:.1f}s"
    assert pool1 <= 500 and st2["pool_size"] <= 500, "池超上限"
    print(
        f"E2E-08: 12s 内 total_pulled 增长 {delta}（≤{max_allowed:.0f}），"
        f"池 {pool1}→{st2['pool_size']}"
    )


# ---------------------------------------------------------------------------
# E2E-09 TTL 到期淘汰（mock 辅助；真实供应商亦返回 expire_time→ttl）
# ---------------------------------------------------------------------------


async def test_e2e_09_ttl_eviction(svc, mock_env):
    """确定性验证 TTL 淘汰：专用 mock level1 + mock_provider 设 ttl=5 → 停供应商 → 二级池记录到期被清。

    使用专用 mock level1（8110）避免共享 mock level1（8100）中 ttl=None 的旧记录干扰。
    """
    mp = BASE["mock_provider"]
    await reset_mock_provider(mp)
    await mock_admin(mp, "/admin/ttl", {"ttl": 5})
    s = Services()
    s.start_uvicorn("mock_l1_ttl", "level1", "level1_mock_ttl.yaml", "LEVEL1_", P_MOCK_LEVEL1_TTL)
    try:
        l1_ttl = f"http://127.0.0.1:{P_MOCK_LEVEL1_TTL}"
        s.wait_http(f"{l1_ttl}/api/v1/status", timeout=90, desc="mock level1 ttl")
        s.start_uvicorn("tmp_ttl_l2", "level2", "level2_ttl.yaml", "LEVEL2_", P_TMP_TTL)
        tmp = f"http://127.0.0.1:{P_TMP_TTL}"
        s.wait_http(f"{tmp}/api/v1/status", timeout=90, desc="tmp ttl level2")
        await wait_until(lambda: level1_size_gte(l1_ttl, 3), 120, 1, "ttl mock level1 pool")
        await wait_until(lambda: pool_total_gte(tmp, 3), 120, 1, "ttl level2 pool fill")
        await mock_admin(mp, "/admin/fail")  # 停供应商 → 无新拉取/刷新
        await wait_until(lambda: pool_total_eq(tmp, 0), 90, 1, "ttl eviction")
        await mock_admin(mp, "/admin/recover")
        await mock_admin(mp, "/admin/ttl", {"ttl": None})
        await wait_until(lambda: pool_total_gte(tmp, 1), 90, 2, "ttl refill")
    finally:
        await mock_admin(mp, "/admin/recover")
        await mock_admin(mp, "/admin/ttl", {"ttl": None})
        s.stop_all()