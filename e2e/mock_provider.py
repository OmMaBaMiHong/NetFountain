"""本地 mock 供应商 + 最小 asyncio 转发代理 + 故障注入控制。

端口约定：
- 供应商 API：:20000  （GET /proxies 供一级池 default_http 拉取）
- 转发代理：27001~27030（127.0.0.1，即 `/proxies` 返回的「IP」）
- mock_site：:9100      （二级池 site_test 的目标站点）

转发代理支持：
- CONNECT host:port —— 上游可达返回 200 并双向转发；上游不可达返回 502
  （真实代理的 lazy-CONNECT 行为：对不可解析目标会关闭隧道，本实现直接回 502）；
- 绝对形式请求（如 ``GET http://127.0.0.1:9100/ HTTP/1.1``）——转发到上游并回传。

故障注入（仅 mock 辅助场景 E2E-05/06/09 使用）：
- POST /admin/down   body ``{"ports":[27001,...]}`` 或 ``{}``(全部)  停掉对应代理端口
- POST /admin/up     body 同上                                       恢复对应代理端口
- POST /admin/fail                                                    供应商 API 返回 500
- POST /admin/recover                                                供应商 API 恢复
- POST /admin/ttl    body ``{"ttl":5}`` / ``{"ttl":null}``            设置/清除返回 ttl
- GET  /admin/state                                                    当前存活端口/ttl/fail 状态
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

HOST = "127.0.0.1"
START_PORT = 27001
N_PROXIES = 30
API_PORT = 20000

_CONNECT_OK = b"HTTP/1.1 200 Connection established\r\n\r\n"
_CONNECT_502 = (
    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
)


@dataclass
class MockState:
    fail_api: bool = False
    ttl: float | None = None
    ports: list[int] = field(default_factory=list)
    alive: set[int] = field(default_factory=set)
    _cursor: int = 0


state = MockState(ports=[START_PORT + i for i in range(N_PROXIES)])
servers: dict[int, asyncio.AbstractServer] = {}
_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# 转发代理
# ---------------------------------------------------------------------------


async def _pump(a_reader: asyncio.StreamReader, a_writer: asyncio.StreamWriter,
                b_reader: asyncio.StreamReader, b_writer: asyncio.StreamWriter) -> None:
    async def _copy(src, dst):
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                dst.close()
            except Exception:
                pass

    t1 = asyncio.create_task(_copy(a_reader, b_writer))
    t2 = asyncio.create_task(_copy(b_reader, a_writer))
    try:
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
    except asyncio.CancelledError:
        for t in (t1, t2):
            t.cancel()
        raise


async def _handle_connect(hostport: str, reader, writer) -> None:
    host, sep, port_str = hostport.rpartition(":")
    if not sep:
        host, port = hostport, 443
    else:
        try:
            port = int(port_str)
        except ValueError:
            port = 443
    up_reader, up_writer = None, None
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3
        )
    except (OSError, asyncio.TimeoutError, ConnectionError, asyncio.CancelledError):
        try:
            writer.write(_CONNECT_502)
            await writer.drain()
        except Exception:
            pass
        return
    try:
        writer.write(_CONNECT_OK)
        await writer.drain()
    except Exception:
        up_writer.close()
        return
    await _pump(reader, writer, up_reader, up_writer)


async def _handle_forward(method: str, target: str, reader, writer) -> None:
    parsed = urlsplit(target)
    host = parsed.hostname
    if not host:
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=3
        )
    except (OSError, asyncio.TimeoutError, ConnectionError, asyncio.CancelledError):
        return
    headers: list[bytes] = []
    while True:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            break
        if not line or line in (b"\r\n", b"\n"):
            break
        headers.append(line)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        up_writer.write(f"{method} {path} HTTP/1.1\r\n".encode("latin-1"))
        for h in headers:
            up_writer.write(h)
        if not any(h.lower().startswith(b"host:") for h in headers):
            up_writer.write(f"Host: {host}:{port}\r\n".encode("latin-1"))
        up_writer.write(b"\r\n")
        await up_writer.drain()
    except (ConnectionError, OSError):
        up_writer.close()
        return
    await _pump(reader, writer, up_reader, up_writer)


async def _handle(reader, writer) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if not line:
            return
        parts = line.decode("latin-1", "replace").strip().split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]
        if method.upper() == "CONNECT":
            await _handle_connect(target, reader, writer)
        else:
            await _handle_forward(method, target, reader, writer)
    except (asyncio.CancelledError, OSError, ConnectionError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def start_proxy(port: int) -> None:
    async def _handler(reader, writer):
        await _handle(reader, writer)

    srv = await asyncio.start_server(_handler, HOST, port)
    servers[port] = srv
    state.alive.add(port)


async def stop_proxy(port: int) -> None:
    srv = servers.pop(port, None)
    if srv is not None:
        srv.close()
        try:
            await srv.wait_closed()
        except Exception:
            pass
    state.alive.discard(port)


# ---------------------------------------------------------------------------
# FastAPI：供应商 API + 控制面
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    for port in state.ports:
        await start_proxy(port)
    try:
        yield
    finally:
        for port in list(servers):
            await stop_proxy(port)


app = FastAPI(title="Mock Provider", lifespan=lifespan)


@app.get("/proxies")
async def proxies(count: int = 10):
    if state.fail_api:
        raise HTTPException(status_code=500, detail="mock provider failure")
    alive = sorted(state.alive)
    if not alive:
        return {"data": []}
    n = min(count, len(alive))
    items = []
    for i in range(n):
        port = alive[(state._cursor + i) % len(alive)]
        items.append(
            {
                "ip": HOST,
                "port": port,
                "protocol": "http",
                "region": "mock",
                "ttl": state.ttl,
            }
        )
    state._cursor = (state._cursor + n) % len(alive)
    return {"data": items}


async def _body_ports(request: Request) -> list[int]:
    raw = await request.body()
    body = await request.json() if raw else {}
    ports = body.get("ports") or []
    return [int(p) for p in ports]


@app.post("/admin/down")
async def admin_down(request: Request):
    ports = await _body_ports(request) or list(state.ports)
    async with _lock:
        for p in ports:
            await stop_proxy(p)
    return {"ok": True, "stopped": ports}


@app.post("/admin/up")
async def admin_up(request: Request):
    ports = await _body_ports(request) or list(state.ports)
    async with _lock:
        for p in ports:
            if p not in state.alive:
                await start_proxy(p)
    return {"ok": True, "started": ports}


@app.post("/admin/fail")
async def admin_fail():
    state.fail_api = True
    return {"ok": True, "fail_api": True}


@app.post("/admin/recover")
async def admin_recover():
    state.fail_api = False
    return {"ok": True, "fail_api": False}


@app.post("/admin/ttl")
async def admin_ttl(request: Request):
    body = await request.json()
    ttl = body.get("ttl")
    state.ttl = float(ttl) if ttl is not None else None
    return {"ok": True, "ttl": state.ttl}


@app.get("/admin/state")
async def admin_state():
    return JSONResponse(
        {
            "ports": state.ports,
            "alive": sorted(state.alive),
            "ttl": state.ttl,
            "fail_api": state.fail_api,
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=API_PORT, log_level="info")