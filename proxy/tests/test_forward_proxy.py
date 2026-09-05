"""forward_proxy 测试：标准正向代理端口 → 二级池 HTTP 租还闭环。

覆盖：
- 普通绝对 URI 请求经上游 IP 转发，坏 IP 自动换下一个（轮换重试）；
- CONNECT 隧道（HTTPS 同款路径）建联后透传到目标服务；
- 用完/失败的 IP 均向二级池 release 归还。

上游「免费代理」与目标服务用线程内 ThreadingHTTPServer 模拟：
- 好代理：支持 CONNECT 盲转发与绝对 URI GET（罐头响应）；
- 坏代理：端口不监听（连接立即被拒，触发轮换）。
"""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from app.config import ForwardProxyConfig, ProxySettings
from app.main import create_app
from app.registry import Registry


# ---------------------------------------------------------------------------
# 客户端辅助（阻塞调用，测试内经 asyncio.to_thread 执行）
# ---------------------------------------------------------------------------


def _proxy_get(port: int, url: str) -> tuple[int, dict]:
    """经正向代理发普通 HTTP 请求。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", url)
    resp = conn.getresponse()
    body = json.loads(resp.read())
    status = resp.status
    conn.close()
    return status, body


def _proxy_tunnel_get(port: int, target_host: str, target_port: int) -> tuple[int, dict, dict]:
    """CONNECT 隧道 + 隧道内 GET。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.set_tunnel(target_host, target_port)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    status = resp.status
    headers = dict(resp.getheaders())
    conn.close()
    return status, body, headers


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


async def test_plain_request_rotates_dead_ip_then_succeeds(
    mock_level2, upstream_and_target, free_port, tmp_path
):
    """普通请求：第一个 IP 是死的 → 自动换好 IP → 拿到响应；租过的 IP 都归还。"""
    ports = upstream_and_target
    async with mock_level2("site_a") as servers:
        site, state, base_url = servers[0]
        # 依次投放：坏 IP、好 IP（mock 的 acquire 端点每次新建记录，改为按序出队）
        crafted = []

        def _push(ip_port):
            rec = dict(state.acquire())
            rec.update(ip="127.0.0.1", port=ip_port, leased=True)
            state.pool[rec["id"]] = rec
            crafted.append(rec)

        _push(ports["dead_port"])
        _push(ports["good_port"])

        def _acquire_in_order():
            if crafted:
                return crafted.pop(0)
            return {"code": 40402, "msg": "empty pool", "data": None}

        state.acquire = _acquire_in_order  # type: ignore[method-assign]

        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump({"sites": [{"name": site, "base_url": base_url}]}),
            encoding="utf-8",
        )
        reg = Registry(route_file=str(routes))
        await reg.load()

        fp_port = free_port()
        settings = ProxySettings(
            forward_proxy=ForwardProxyConfig(
                enabled=True,
                host="127.0.0.1",
                port=fp_port,
                max_attempts=3,
                connect_timeout=2.0,
                upstream_timeout=5.0,
                acquire_max_wait=2.0,
                acquire_interval=0.1,
            )
        )
        app = create_app(settings, registry=reg, start_reload=False)
        async with app.router.lifespan_context(app):
            status, body = await asyncio.to_thread(
                _proxy_get, fp_port, f"http://127.0.0.1:{ports['target_port']}/x"
            )
        assert status == 200
        assert body["via"] == "mock-proxy-plain"
        # 等代理侧归还 IP 的收尾协程执行完
        await asyncio.sleep(0.3)
        # 两个租过的 IP（坏的+好的）都应已归还
        assert all(r["leased"] is False for r in state.pool.values())


async def test_connect_tunnel_via_pool_ip(mock_level2, upstream_and_target, free_port, tmp_path):
    """CONNECT 隧道：踩一个坏 IP 后轮换到好 IP，隧道内拿到目标服务响应。"""
    ports = upstream_and_target
    async with mock_level2("site_a") as servers:
        site, state, base_url = servers[0]
        crafted = []

        def _push(ip_port):
            rec = dict(state.acquire())
            rec.update(ip="127.0.0.1", port=ip_port, leased=True)
            state.pool[rec["id"]] = rec
            crafted.append(rec)

        _push(ports["dead_port"])
        _push(ports["good_port"])

        def _acquire_in_order():
            if crafted:
                return crafted.pop(0)
            return {"code": 40402, "msg": "empty pool", "data": None}

        state.acquire = _acquire_in_order  # type: ignore[method-assign]

        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump({"sites": [{"name": site, "base_url": base_url}]}),
            encoding="utf-8",
        )
        reg = Registry(route_file=str(routes))
        await reg.load()

        fp_port = free_port()
        settings = ProxySettings(
            forward_proxy=ForwardProxyConfig(
                enabled=True,
                host="127.0.0.1",
                port=fp_port,
                max_attempts=3,
                connect_timeout=2.0,
                upstream_timeout=5.0,
                acquire_max_wait=2.0,
                acquire_interval=0.1,
            )
        )
        app = create_app(settings, registry=reg, start_reload=False)
        async with app.router.lifespan_context(app):
            status, body, headers = await asyncio.to_thread(
                _proxy_tunnel_get, fp_port, "127.0.0.1", ports["target_port"]
            )
        assert status == 200
        assert body["via"] == "mock-target"
        assert headers.get("X-Mock-Target") == "yes"
        await asyncio.sleep(1.5)
        print("POOL_AFTER_1.5s:", {k: (v["port"], v["leased"]) for k, v in state.pool.items()})
        assert all(r["leased"] is False for r in state.pool.values())


async def test_all_attempts_fail_returns_502(mock_level2, upstream_and_target, free_port, tmp_path):
    """所有 IP 都是死的 → 客户端收到 502，且每个 IP 都被归还。"""
    ports = upstream_and_target
    async with mock_level2("site_a") as servers:
        site, state, base_url = servers[0]
        crafted = []

        def _push(ip_port):
            rec = dict(state.acquire())
            rec.update(ip="127.0.0.1", port=ip_port, leased=True)
            state.pool[rec["id"]] = rec
            crafted.append(rec)

        _push(ports["dead_port"])
        _push(ports["dead_port"])
        _push(ports["dead_port"])

        def _acquire_in_order():
            if crafted:
                return crafted.pop(0)
            return {"code": 40402, "msg": "empty pool", "data": None}

        state.acquire = _acquire_in_order  # type: ignore[method-assign]

        routes = tmp_path / "routes.yaml"
        routes.write_text(
            yaml.safe_dump({"sites": [{"name": site, "base_url": base_url}]}),
            encoding="utf-8",
        )
        reg = Registry(route_file=str(routes))
        await reg.load()

        fp_port = free_port()
        settings = ProxySettings(
            forward_proxy=ForwardProxyConfig(
                enabled=True,
                host="127.0.0.1",
                port=fp_port,
                max_attempts=3,
                connect_timeout=2.0,
                upstream_timeout=5.0,
                acquire_max_wait=1.0,
                acquire_interval=0.1,
            )
        )
        app = create_app(settings, registry=reg, start_reload=False)

        def _tunnel_expect_502():
            conn = http.client.HTTPConnection("127.0.0.1", fp_port, timeout=20)
            conn.set_tunnel("127.0.0.1", ports["target_port"])
            conn.request("GET", "/")
            resp = conn.getresponse()
            body = json.loads(resp.read())
            status = resp.status
            conn.close()
            return status, body

        async with app.router.lifespan_context(app):
            # http.client 对 CONNECT 非 200 抛 OSError，信息即代理回的 502 原因
            with pytest.raises(OSError, match="all upstream proxies failed"):
                await asyncio.to_thread(_tunnel_expect_502)
        await asyncio.sleep(0.3)
        assert all(r["leased"] is False for r in state.pool.values())
