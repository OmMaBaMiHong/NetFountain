"""tunnel 测试：隧道代理入口（一个端口 + 凭据）→ 二级池租还闭环。

覆盖：
- 带账号凭据：普通绝对 URI 请求经出口 IP 转发、CONNECT 隧道到目标服务；
- 坏 IP 自动轮换到好 IP；全部失败回 502；
- 无凭据走默认池；凭据错误回 407（带 Proxy-Authenticate）；
- 租过的 IP 全部归还。

上游「免费代理」与目标服务用线程内 ThreadingHTTPServer 模拟：
- 好代理：支持 CONNECT 盲转发与绝对 URI GET（罐头响应）；
- 坏代理：端口不监听（连接立即被拒，触发轮换）。
"""
from __future__ import annotations

import asyncio
import base64
import http.client
import json
from contextlib import asynccontextmanager

import yaml

from app.config import AuthConfig, ProxySettings, TunnelConfig
from app.main import create_app
from app.registry import Registry


# ---------------------------------------------------------------------------
# 客户端辅助（阻塞调用，测试内经 asyncio.to_thread 执行）
# ---------------------------------------------------------------------------


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def _proxy_get(port: int, url: str, auth: str | None = None) -> tuple[int, dict]:
    """经隧道入口发普通 HTTP 请求（绝对 URI）。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Proxy-Authorization": auth} if auth else {}
    conn.request("GET", url, headers=headers)
    resp = conn.getresponse()
    body = resp.read()
    status = resp.status
    conn.close()
    return status, json.loads(body) if body else {}


def _tunnel_get(port: int, host: str, target_port: int,
                auth: str | None = None) -> tuple[int, dict, dict]:
    """CONNECT 隧道 + 隧道内 GET。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    headers = {"Proxy-Authorization": auth} if auth else {}
    conn.set_tunnel(host, target_port, headers=headers)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    status = resp.status
    resp_headers = dict(resp.getheaders())
    conn.close()
    return status, body, resp_headers


def _craft_pool(state, ports: list[int]) -> None:
    """向 mock 二级池按序投放 127.0.0.1 出口记录，acquire 依次出队。"""
    crafted = []

    def _push(port):
        rec = dict(state.acquire())
        rec.update(ip="127.0.0.1", port=port, leased=True)
        state.pool[rec["id"]] = rec
        crafted.append(rec)

    for port in ports:
        _push(port)

    def _acquire_in_order():
        if crafted:
            return crafted.pop(0)
        return {"code": 40402, "msg": "empty pool", "data": None}

    state.acquire = _acquire_in_order  # type: ignore[method-assign]


@asynccontextmanager
async def _tunnel_app(mock_level2, upstream_and_target, free_port, tmp_path):
    """起 site_a 的 mock 二级池 + 隧道代理应用，yield (client, site, state, ports, app)。"""
    ports = upstream_and_target
    async with mock_level2("site_a") as servers:
        site, state, base_url = servers[0]
        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump({"sites": [{"name": site, "base_url": base_url}]}),
            encoding="utf-8",
        )
        reg = Registry(route_file=str(routes))
        await reg.load()

        settings = ProxySettings(
            auth=AuthConfig(default_site="site_a",
                            db_path=str(tmp_path / "accounts.db")),
            tunnel=TunnelConfig(
                enabled=True, host="127.0.0.1", port=free_port(),
                max_attempts=3, connect_timeout=2.0, upstream_timeout=5.0,
                acquire_max_wait=2.0, acquire_interval=0.1,
            ),
        )
        app = create_app(settings, registry=reg, start_reload=False)
        async with app.router.lifespan_context(app):
            import httpx

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://test") as client:
                yield client, site, state, ports, app


async def _register(client, site: str) -> None:
    r = await client.post(
        "/api/v1/accounts",
        json={"username": "u1", "password": "pw", "assigned_site": site},
    )
    assert r.json()["code"] == 0


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def test_plain_request_with_account_releases_ip(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """带凭据普通请求：经出口 IP 转发成功，租过的 IP 都归还。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        await _register(client, site)
        _craft_pool(state, [ports["good_port"]])
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x", _basic("u1", "pw"),
        )
        assert status == 200
        assert body["via"] == "mock-proxy-plain"
        await asyncio.sleep(0.3)  # 等归还收尾协程执行完
        assert all(rec["leased"] is False for rec in state.pool.values())


async def test_connect_tunnel_with_account(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """带凭据 CONNECT 隧道：隧道内拿到目标服务响应，租过的 IP 都归还。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        await _register(client, site)
        _craft_pool(state, [ports["good_port"]])
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body, headers = await asyncio.to_thread(
            _tunnel_get, tunnel_port,
            "127.0.0.1", ports["target_port"], _basic("u1", "pw"),
        )
        assert status == 200
        assert body["via"] == "mock-target"
        assert headers.get("X-Mock-Target") == "yes"
        await asyncio.sleep(1.5)
        assert all(rec["leased"] is False for rec in state.pool.values())


async def test_dead_ip_rotates_then_succeeds(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """坏 IP 自动轮换：第一个 IP 是死的，换到好 IP 后成功。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        await _register(client, site)
        _craft_pool(state, [ports["dead_port"], ports["good_port"]])
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x", _basic("u1", "pw"),
        )
        assert status == 200
        assert body["via"] == "mock-proxy-plain"
        await asyncio.sleep(0.3)
        assert all(rec["leased"] is False for rec in state.pool.values())


async def test_all_upstreams_fail_returns_502(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """所有出口 IP 都是死的 → 502，且每个 IP 都被归还。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        await _register(client, site)
        _craft_pool(state, [ports["dead_port"]] * 3)
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x", _basic("u1", "pw"),
        )
        assert status == 502
        assert body["error"] == "all upstream proxies failed"
        await asyncio.sleep(0.3)
        assert all(rec["leased"] is False for rec in state.pool.values())


async def test_no_credentials_uses_default_pool(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """无凭据：走默认池（default_site=site_a），成功转发。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        _craft_pool(state, [ports["good_port"]])
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x",
        )
        assert status == 200
        assert body["via"] == "mock-proxy-plain"
        await asyncio.sleep(0.3)
        assert all(rec["leased"] is False for rec in state.pool.values())


async def test_bad_credentials_return_407(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """凭据错误/格式非法 → 407，不租 IP。"""
    async with _tunnel_app(mock_level2, upstream_and_target,
                           free_port, tmp_path) as (client, site, state, ports, app):
        _craft_pool(state, [ports["good_port"]])
        for rec in state.pool.values():  # 407 场景应零租出，先清掉投放时的 leased 标记
            rec["leased"] = False
        tunnel_port = int(app.state.tunnel.bound.split(":")[1])

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x", _basic("u1", "wrong"),
        )
        assert status == 407
        assert body["error"] == "proxy authentication required"

        status, body = await asyncio.to_thread(
            _proxy_get, tunnel_port,
            f"http://127.0.0.1:{ports['target_port']}/x", "Digest xyz",
        )
        assert status == 407

        # 407 不租 IP：池里的记录保持未租出
        assert all(rec["leased"] is False for rec in state.pool.values())
