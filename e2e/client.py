"""e2e HTTP 客户端、统计与等待工具。"""
from __future__ import annotations

import asyncio
import time

import aiohttp
import httpx
from aiohttp_socks import ProxyConnector


# ---------------------------------------------------------------------------
# 基础 API 调用（httpx）
# ---------------------------------------------------------------------------


async def api_get(client: httpx.AsyncClient, url: str):
    resp = await client.get(url)
    return resp.status_code, resp.json()


async def api_post(client: httpx.AsyncClient, url: str, json_body: dict | None = None):
    resp = await client.post(url, json=json_body) if json_body is not None else await client.post(url)
    return resp.status_code, resp.json()


async def api_delete(client: httpx.AsyncClient, url: str):
    resp = await client.delete(url)
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# 状态查询（各服务）
# ---------------------------------------------------------------------------


async def level1_status(base: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        r = await c.get("/api/v1/status")
        return r.json().get("data") or {}


async def level1_pool_size(base: str) -> int:
    return (await level1_status(base)).get("pool_size", -1)


async def level1_total_pulled(base: str) -> int:
    return (await level1_status(base)).get("total_pulled", -1)


async def level2_status(base: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        r = await c.get("/api/v1/status")
        return r.json().get("data") or {}


async def pool_stats(base: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        r = await c.get("/api/v1/count")
        return r.json().get("data") or {}


async def pool_total(base: str) -> int:
    return (await pool_stats(base)).get("total", -1)


async def pool_free(base: str) -> int:
    return (await pool_stats(base)).get("free_total", -1)


async def pool_ips(base: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        r = await c.get("/api/v1/ips")
        return r.json().get("data") or []


# ---------------------------------------------------------------------------
# 等待与统计
# ---------------------------------------------------------------------------


async def wait_until(pred_async, timeout: float, interval: float = 1.0, desc: str = ""):
    """轮询异步谓词直至返回真值；超时抛异常。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = await pred_async()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        await asyncio.sleep(interval)
    raise TimeoutError(f"timeout ({timeout}s): {desc}; last={last!r}")


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (s[c] - s[f]) * (k - f)


# 供 wait_until 使用的「协程返回型」谓词（避免 lambda 内对协程做同步比较）
async def pool_total_gte(base: str, n: int) -> bool:
    return (await pool_total(base)) >= n


async def pool_total_eq(base: str, n: int) -> bool:
    return (await pool_total(base)) == n


async def pool_free_gte(base: str, n: int) -> bool:
    return (await pool_free(base)) >= n


async def level1_size_gte(base: str, n: int) -> bool:
    return (await level1_pool_size(base)) >= n


async def level1_pulled_gte(base: str, n: int) -> bool:
    return (await level1_total_pulled(base)) >= n


async def level2_synced_lt(base: str, old: int) -> bool:
    """二级池水位线已重置进新 id 空间（重启后 id 归零 → 新水位线 < 旧值）。"""
    v = (await level2_status(base)).get("last_synced_id")
    return v is not None and v < old


# ---------------------------------------------------------------------------
# 出口请求（经代理）
# ---------------------------------------------------------------------------


async def proxy_egress(proxy_url: str, target_url: str, timeout: float = 8.0):
    """经代理请求目标，收到任意 HTTP 响应即 (True, status)；连接/代理错误 (False, None)。"""
    connector = ProxyConnector.from_url(proxy_url)
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.get(
                target_url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                await resp.read()
                return True, resp.status
        except Exception:  # noqa: BLE001
            return False, None


# ---------------------------------------------------------------------------
# mock 供应商控制面
# ---------------------------------------------------------------------------


async def mock_admin(base: str, path: str, json_body: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=15) as c:
        if json_body is None:
            r = await c.post(path)
        else:
            r = await c.post(path, json=json_body)
        r.raise_for_status()
        return r.json()


async def mock_state(base: str) -> dict:
    async with httpx.AsyncClient(base_url=base, timeout=10) as c:
        r = await c.get("/admin/state")
        r.raise_for_status()
        return r.json()


async def reset_mock_provider(base: str) -> None:
    """恢复 mock 供应商到干净状态：全部端口在线、无故障、无 ttl。"""
    await mock_admin(base, "/admin/recover")
    await mock_admin(base, "/admin/ttl", {"ttl": None})
    await mock_admin(base, "/admin/up", {})