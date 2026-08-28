"""性能测试（marker perf，默认跳过，用 -m perf 运行）：高并发透传不串扰、吞吐稳定。

覆盖测试计划书 PX-PERF-001。
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.perf

PAYLOAD_A = {"code": 0, "msg": "ok", "data": {"site": "site_a"}}
PAYLOAD_B = {"code": 0, "msg": "ok", "data": {"site": "site_b"}}


@pytest.mark.perf
async def test_high_concurrency_passthrough(dispatcher, aio_mock):
    """200 并发透传：结果不串扰、全部成功、耗时稳定。"""
    aio_mock.get(
        "http://127.0.0.1:8001/api/v1/ips", payload=PAYLOAD_A, status=200, repeat=True
    )
    aio_mock.get(
        "http://127.0.0.1:8002/api/v1/ips", payload=PAYLOAD_B, status=200, repeat=True
    )

    async def call(site: str):
        _, body = await dispatcher.forward(site, "GET", f"/api/v1/{site}/ips")
        return site, body["data"]["site"]

    start = time.perf_counter()
    results = await asyncio.gather(
        *[call("site_a" if i % 2 == 0 else "site_b") for i in range(200)]
    )
    elapsed = time.perf_counter() - start

    assert len(results) == 200
    for i, (requested, returned) in enumerate(results):
        expected = "site_a" if i % 2 == 0 else "site_b"
        assert requested == expected == returned
    assert elapsed < 10.0