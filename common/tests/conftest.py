"""公共库测试 conftest：网络全部打桩，不依赖真实外网。

提供：
- 会话级 event_loop
- 基于 aioresponses 的 mock_session fixture（控制代理/站点 HTTP 响应）
- 本地 SOCKS 桩服务器 fixture（正常/拒绝/超时字节流）
- 临时 yaml 文件 fixture
- aioresponses × aiohttp 3.14 兼容 shim
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest import mock

import aiohttp
import aioresponses
import aioresponses.core
import pytest

# ---------------------------------------------------------------------------
# aioresponses 0.7.9 与 aiohttp 3.14 兼容 shim
# aiohttp 3.14 的 ClientResponse.__init__ 新增必填参数 stream_writer，
# aioresponses 尚未适配。此处注入一个合法的 StreamWriter 使其正常工作。
# ---------------------------------------------------------------------------
from aiohttp.http_writer import StreamWriter


class _CompatClientResponse(aioresponses.core.ClientResponse):
    def __init__(self, method, url, **kwargs):  # noqa: ANN001
        if "stream_writer" not in kwargs:
            kwargs["stream_writer"] = StreamWriter(
                mock.MagicMock(), kwargs.get("loop")
            )
        super().__init__(method, url, **kwargs)


aioresponses.core.ClientResponse = _CompatClientResponse


@pytest.fixture(scope="session")
def event_loop():
    """会话级事件循环，供全部 asyncio 测试复用。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def aio_mock():
    """aioresponses 上下文：测试内通过 m.get(url, ...) 打桩。"""
    with aioresponses.aioresponses() as m:
        yield m


@pytest.fixture
async def mock_session(aio_mock):
    """基于 aioresponses 的 aiohttp 桩会话：可控制代理/站点 HTTP 响应。"""
    async with aiohttp.ClientSession() as session:
        yield session, aio_mock


# ---------------------------------------------------------------------------
# 本地 SOCKS 桩服务器
# ---------------------------------------------------------------------------
_SOCKS5_GREET_OK = b"\x05\x00"
_SOCKS5_CONNECT_OK = b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00"
_SOCKS5_CONNECT_REFUSE = b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00"
_SOCKS4_CONNECT_OK = b"\x00\x5a\x00\x00\x00\x00\x00\x00"
_SOCKS4_CONNECT_REFUSE = b"\x00\x5b\x00\x00\x00\x00\x00\x00"
_HTTP_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"


async def _handle_socks(reader, writer, version: str, mode: str) -> None:
    try:
        data = await asyncio.wait_for(reader.read(4096), 2)
        if version == "socks4":
            if not data or data[0] != 0x04:
                return
            if mode == "timeout":
                await asyncio.sleep(10)
                return
            if mode == "refuse":
                writer.write(_SOCKS4_CONNECT_REFUSE)
                await writer.drain()
                return
            writer.write(_SOCKS4_CONNECT_OK)
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), 2)
            writer.write(_HTTP_OK)
            await writer.drain()
            await asyncio.sleep(0.1)
        else:  # socks5
            if not data or data[0] != 0x05:
                return
            if mode == "timeout":
                await asyncio.sleep(10)
                return
            writer.write(_SOCKS5_GREET_OK)
            await writer.drain()
            data = await asyncio.wait_for(reader.read(4096), 2)
            if mode == "refuse":
                writer.write(_SOCKS5_CONNECT_REFUSE)
                await writer.drain()
                return
            writer.write(_SOCKS5_CONNECT_OK)
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), 2)
            writer.write(_HTTP_OK)
            await writer.drain()
            await asyncio.sleep(0.1)
    except (asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        writer.close()


@pytest.fixture
async def socks_server():
    """本地 SOCKS 桩服务器：mode ∈ normal/refuse/timeout，version ∈ socks4/socks5。

    返回 async 工厂函数 ``(version, mode) -> url``。
    """
    servers: list[asyncio.AbstractServer] = []

    async def _factory(version: str = "socks5", mode: str = "normal") -> str:
        async def _handler(reader, writer):
            await _handle_socks(reader, writer, version, mode)

        srv = await asyncio.start_server(_handler, "127.0.0.1", 0)
        servers.append(srv)
        port = srv.sockets[0].getsockname()[1]
        return f"{version}://127.0.0.1:{port}"

    yield _factory

    for srv in servers:
        srv.close()
        await srv.wait_closed()


@pytest.fixture
def tmp_yaml(tmp_path):
    """临时 yaml 文件工厂：``tmp_yaml(content, name="config.yaml") -> path``。"""

    def _make(content: str, name: str = "config.yaml") -> str:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    return _make


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """默认跳过 perf 标记的测试，除非显式使用 -m perf 运行。"""
    markexpr = config.getoption("-m") or ""
    if "perf" in markexpr:
        return
    skip_perf = pytest.mark.skip(reason="perf 测试默认跳过，使用 -m perf 运行")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip_perf)