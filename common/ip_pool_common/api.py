"""API 通用件：统一错误码、响应封装、调用计数中间件、统一启动入口。"""
from __future__ import annotations

import threading
from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    """统一错误码。"""

    OK = 0
    PARAM_ERROR = 40000
    NOT_FOUND = 40400          # 站点未配置/对象不存在
    EMPTY_POOL = 40402         # 二级池 acquire 空池
    INTERNAL = 50000
    UPSTREAM_ERROR = 50200     # 代理层转发上游故障


def ok(data: Any = None) -> dict[str, Any]:
    """统一成功响应：``{"code": 0, "msg": "ok", "data": data}``。"""
    return {"code": ErrorCode.OK, "msg": "ok", "data": data}


def err(code: int | ErrorCode, msg: str) -> dict[str, Any]:
    """统一失败响应：``{"code": code, "msg": msg, "data": None}``。"""
    return {"code": int(code), "msg": msg, "data": None}


class ApiCounterMiddleware:
    """API 调用计数中间件（纯 ASGI，thread/async 安全）。

    用法：``app.add_middleware(ApiCounterMiddleware)``，
    通过 ``middleware.count`` 读取累计调用次数。
    """

    def __init__(self, app: Any) -> None:
        self.app = app
        self._lock = threading.Lock()
        self._count = 0

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            with self._lock:
                self._count += 1
        await self.app(scope, receive, send)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


async def run_app(app: Any, host: str, port: int) -> None:
    """统一 uvicorn 启动入口（uvicorn 延迟导入）。"""
    import uvicorn  # noqa: PLC0415

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()