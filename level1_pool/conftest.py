"""Phase 0 conftest：提供会话级 event_loop 与 tmp_config fixture，并默认跳过 perf 测试。"""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop():
    """会话级事件循环，供 asyncio 测试复用。"""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def tmp_config(tmp_path):
    """生成临时配置文件目录（扩展点：可写入临时 yaml 供测试使用）。"""
    return tmp_path


def pytest_collection_modifyitems(config, items):
    """默认跳过 perf 标记的测试，除非显式使用 -m perf 运行。"""
    markexpr = config.getoption("-m") or ""
    if "perf" in markexpr:
        return
    skip_perf = pytest.mark.skip(reason="perf 测试默认跳过，使用 -m perf 运行")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip_perf)