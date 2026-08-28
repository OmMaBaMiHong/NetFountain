"""e2e pytest 配置：session 级进程管理 fixture + perf 标记默认跳过。"""
from __future__ import annotations

import pytest

from processes import Services, start_mock_env, start_real_chain


@pytest.fixture(scope="session")
def svc():
    """真实链路进程（level1 / level2×2 / proxy），session 级；就绪仅指 HTTP 可访问。"""
    s = Services()
    start_real_chain(s)
    try:
        yield s
    finally:
        s.stop_all()


@pytest.fixture(scope="session")
def mock_env():
    """mock 辅助进程（mock_site / mock_provider / mock level1），仅 mock 场景使用。"""
    s = Services()
    start_mock_env(s)
    try:
        yield s
    finally:
        s.stop_all()


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    """默认跳过 perf 标记的测试，除非显式使用 -m perf 运行。"""
    markexpr = config.getoption("-m") or ""
    if "perf" in markexpr:
        return
    skip_perf = pytest.mark.skip(reason="perf 测试默认跳过，使用 -m perf 运行")
    for item in items:
        if "perf" in item.keywords:
            item.add_marker(skip_perf)