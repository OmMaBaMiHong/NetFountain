"""gateway 测试：对外单端口协议分拣。

同一端口上：
- 代理协议请求（CONNECT / 绝对 URI）→ ForwardProxyServer（租 IP 转发）；
- 相对路径请求（/api/v1/...）→ 转交回环 uvicorn 管理 API。
"""
from __future__ import annotations

import asyncio
import http.client
import json

import pytest
import uvicorn
import yaml

from app.config import ForwardProxyConfig, ProxySettings
from app.gateway import GatewayServer
from app.forward_proxy import ForwardProxyServer
from app.main import create_app
from app.registry import Registry


def _craft_pool(state, ports: list[int]):
    """向 mock 二级池按序投放 127.0.0.1 租赁记录，acquire 依次出队。"""
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


async def _start_gateway(settings, registry, session):
    """在测试事件循环里起 uvicorn(回环) + 网关，返回 (gateway, server, task)。"""
    app = create_app(settings, registry=registry, session=session,
                     start_reload=False, start_forward_proxy=False)
    config = uvicorn.Config(app, host="127.0.0.1",
                            port=settings.forward_proxy.internal_port,
                            log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.02)
    assert server.started, "internal api server failed to start"

    proxy = ForwardProxyServer(registry, session, settings)
    proxy.validate()
    gateway = GatewayServer(proxy, settings.forward_proxy.internal_port)
    await gateway.start()
    return gateway, server, task


def _get_raw(port: int, path: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = json.loads(resp.read())
    status = resp.status
    conn.close()
    return status, body


async def test_single_port_serves_both_proxy_and_api(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """同一个端口：CONNECT 走代理隧道、绝对 URI 走代理转发、/api/v1 走管理 API。"""
    ports = upstream_and_target
    async with mock_level2("site_a") as servers:
        site, state, base_url = servers[0]
        _craft_pool(state, [ports["dead_port"], ports["good_port"],
                            ports["dead_port"], ports["good_port"]])

        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump({"sites": [{"name": site, "base_url": base_url}]}),
            encoding="utf-8",
        )
        reg = Registry(route_file=str(routes))
        await reg.load()

        shared_port = free_port()
        settings = ProxySettings(
            forward_proxy=ForwardProxyConfig(
                enabled=True,
                host="127.0.0.1",
                port=shared_port,  # 与 service.port 相同 → 网关模式
                internal_port=free_port(),
                max_attempts=3,
                connect_timeout=2.0,
                upstream_timeout=5.0,
                acquire_max_wait=2.0,
                acquire_interval=0.1,
            )
        )

        import aiohttp

        async with aiohttp.ClientSession() as session:
            gateway, server, task = await _start_gateway(settings, reg, session)
            try:
                # 1) 管理 API：相对路径 → 转交 uvicorn
                status, body = await asyncio.to_thread(
                    _get_raw, shared_port, f"/api/v1/{site}/status"
                )
                assert status == 200
                assert body["code"] == 0
                assert body["data"]["site"] == site

                # 2) 代理协议：CONNECT 隧道（踩坏 IP 后轮换到好 IP）
                def _tunnel():
                    conn = http.client.HTTPConnection("127.0.0.1", shared_port, timeout=20)
                    conn.set_tunnel("127.0.0.1", ports["target_port"])
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    data = json.loads(resp.read())
                    status_ = resp.status
                    conn.close()
                    return status_, data

                status, body = await asyncio.to_thread(_tunnel)
                assert status == 200
                assert body["via"] == "mock-target"

                # 3) 代理协议：绝对 URI GET
                def _plain():
                    conn = http.client.HTTPConnection("127.0.0.1", shared_port, timeout=20)
                    conn.request("GET", f"http://127.0.0.1:{ports['target_port']}/x")
                    resp = conn.getresponse()
                    data = json.loads(resp.read())
                    status_ = resp.status
                    conn.close()
                    return status_, data

                status, body = await asyncio.to_thread(_plain)
                assert status == 200
                assert body["via"] == "mock-proxy-plain"

                # 租过的 IP 均已归还
                await asyncio.sleep(0.3)
                assert all(r["leased"] is False for r in state.pool.values())
            finally:
                await gateway.close()
                server.should_exit = True
                try:
                    await asyncio.wait_for(task, timeout=3)
                except asyncio.TimeoutError:
                    task.cancel()
