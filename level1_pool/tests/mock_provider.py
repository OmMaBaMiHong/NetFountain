"""本地模拟供应商：FastAPI 服务，返回与 provider.py 相同格式的 JSON。

可配置返回数量 / 协议 / 失败率（HTTP 500）/ 响应延迟，供集成与性能测试。
通过模块级 ``state``（MockState）在测试中修改行为。
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException


@dataclass
class MockState:
    """可变配置：测试中直接修改该对象控制模拟供应商行为。"""

    count: int = 10
    protocol: str = "http"
    protocols: list[str] | None = None
    failure_rate: float = 0.0
    delay: float = 0.0
    ttl: float | None = 120
    region: str = "mock-region"
    _seq: int = field(default=0, init=False, repr=False)

    def reset(self) -> None:
        self.count = 10
        self.protocol = "http"
        self.protocols = None
        self.failure_rate = 0.0
        self.delay = 0.0
        self.ttl = 120
        self.region = "mock-region"
        self._seq = 0

    def next_endpoint(self) -> tuple[str, int]:
        """返回互不重复的 ip:port 端点（跨请求持续递增）。"""
        self._seq += 1
        octet = 1 + (self._seq % 250)
        port = 8000 + (self._seq // 250) % 1000
        return f"127.0.0.{octet}", port


state = MockState()

app = FastAPI(title="Mock Provider")


@app.get("/proxies")
async def proxies(count: int = 10):
    if state.delay > 0:
        await asyncio.sleep(state.delay)
    if random.random() < state.failure_rate:
        raise HTTPException(status_code=500, detail="mock provider failure")
    n = min(count, state.count)
    protocols = state.protocols or [state.protocol]
    items = []
    for i in range(n):
        ip, port = state.next_endpoint()
        items.append(
            {
                "ip": ip,
                "port": port,
                "protocol": protocols[i % len(protocols)],
                "region": state.region,
                "ttl": state.ttl,
            }
        )
    return {"data": items}
