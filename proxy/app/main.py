"""FastAPI 装配：lifespan 创建/关闭 Registry/Dispatcher/session，启停路由表重载任务。

- 组件可注入（供测试）；未注入的在 lifespan 内按配置创建；
- 首次 load() 读取路由表，随后 reload_loop 每分钟重载（失败保留旧表）；
- 模块级 ``app`` 供 ``uvicorn app.main:app`` 使用（自动加载 config/proxy_routes.yaml）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import aiohttp
from fastapi import FastAPI

from .config import ProxySettings, load_proxy_settings
from .dispatcher import Dispatcher
from .registry import Registry
from .routes import router

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "proxy_routes.yaml"
)


def create_app(
    settings: ProxySettings | None = None,
    *,
    registry: Registry | None = None,
    session: aiohttp.ClientSession | None = None,
    start_reload: bool = True,
) -> FastAPI:
    """装配 FastAPI 应用；组件可注入，未注入的在 lifespan 内按配置创建。"""
    if settings is None:
        if os.path.exists(_CONFIG_PATH):
            settings = load_proxy_settings(_CONFIG_PATH)
        else:
            settings = load_proxy_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        own_session = session is None
        active_session = session if session is not None else aiohttp.ClientSession()
        active_registry: Registry | None = None
        reload_task = None
        try:
            route_file = settings.registry.route_file or None
            if route_file and not os.path.isabs(route_file) and os.path.exists(_CONFIG_PATH):
                route_file = os.path.abspath(
                    os.path.join(os.path.dirname(os.path.dirname(_CONFIG_PATH)), route_file)
                )
            active_registry = registry if registry is not None else Registry(
                route_file=route_file,
                route_url=settings.registry.route_url or None,
                reload_interval=settings.registry.reload_interval,
            )

            dispatcher = Dispatcher(
                active_registry, active_session, timeout=settings.dispatch.timeout
            )
            if active_registry.route_file or active_registry.route_url:
                await active_registry.load()

            if start_reload:
                reload_task = asyncio.create_task(active_registry.reload_loop())

            app.state.settings = settings
            app.state.registry = active_registry
            app.state.dispatcher = dispatcher
            yield
        finally:
            if reload_task is not None:
                reload_task.cancel()
                try:
                    await reload_task
                except (asyncio.CancelledError, Exception):
                    pass
            if active_registry is not None:
                await active_registry.close()
            if own_session:
                await active_session.close()

    app = FastAPI(title="Proxy Gateway", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    return app


app = create_app()
