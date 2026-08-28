"""api.py 测试：响应封装、错误码、计数中间件、统一启动入口。"""
from __future__ import annotations

import asyncio

import aiohttp
import pytest

from ip_pool_common.api import ApiCounterMiddleware, ErrorCode, err, ok, run_app


def test_ok_wraps_data():
    assert ok({"a": 1}) == {"code": 0, "msg": "ok", "data": {"a": 1}}


def test_ok_default_data_none():
    assert ok() == {"code": 0, "msg": "ok", "data": None}


def test_err_wraps_message():
    assert err(40000, "bad param") == {"code": 40000, "msg": "bad param", "data": None}


def test_err_accepts_errorcode_enum():
    assert err(ErrorCode.NOT_FOUND, "no site") == {"code": 40400, "msg": "no site", "data": None}


def test_errorcode_values_unique():
    values = [e.value for e in ErrorCode]
    assert len(values) == len(set(values))


def test_errorcode_expected_values():
    assert ErrorCode.OK == 0
    assert ErrorCode.PARAM_ERROR == 40000
    assert ErrorCode.NOT_FOUND == 40400
    assert ErrorCode.EMPTY_POOL == 40402
    assert ErrorCode.INTERNAL == 50000
    assert ErrorCode.UPSTREAM_ERROR == 50200


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _send(message):
    pass


async def _fake_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_api_counter_increments():
    mw = ApiCounterMiddleware(_fake_app)
    scope = {"type": "http", "method": "GET", "path": "/health"}
    assert mw.count == 0
    for _ in range(3):
        await mw(scope, _receive, _send)
    assert mw.count == 3


async def test_api_counter_ignores_lifespan():
    mw = ApiCounterMiddleware(_fake_app)
    await mw({"type": "lifespan"}, _receive, _send)
    assert mw.count == 0


async def test_run_app_starts_and_serves():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    port = _free_port()
    task = asyncio.create_task(run_app(app, "127.0.0.1", port))
    try:
        async with aiohttp.ClientSession() as session:
            await _wait_for_server(session, port)
            async with session.get(
                f"http://127.0.0.1:{port}/health"
            ) as resp:
                assert resp.status == 200
                assert await resp.json() == {"status": "ok"}
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait_for_server(session, port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with session.get(
                f"http://127.0.0.1:{port}/health",
                timeout=aiohttp.ClientTimeout(total=0.5),
            ) as resp:
                if resp.status == 200:
                    return
        except (aiohttp.ClientError, OSError):
            await asyncio.sleep(0.1)
    pytest.fail("server did not start in time")