"""单端口网关：对外只暴露 service.port（默认 9000）一个端口，内部按协议分拣。

- 请求进来先读第一个 HTTP 头部块，看请求行：
  - ``CONNECT host:port`` 或 ``GET http://host/...``（绝对 URI）→ 代理协议，
    交给 ForwardProxyServer 处理（向站点二级池租 IP 转发）；
  - 其余（相对路径，如 ``/api/v1/zhihu/status``）→ 管理 API，
    原样转交本机回环上的 uvicorn（internal_port，仅 127.0.0.1，外部不可见）；
- 对外永远只有一个端口，下游服务（sub2api / 榜天下 / skoob…）用
  ``auth_user``/``auth_pass`` 区分账号，无需为每个服务加端口。

启动方式（网关模式，forward_proxy.port == service.port 时必须用它代替 uvicorn）：

    cd proxy && python -m app.gateway
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

from ip_pool_common.config import load_yaml

from .config import ProxySettings, load_proxy_settings
from .forward_proxy import ForwardProxyServer, HEAD_LIMIT
from .registry import Registry

logger = logging.getLogger(__name__)

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "proxy_routes.yaml"
)

_BACKEND_TIMEOUT = 10.0


class _ReplayReader:
    """把网关已读出的头部块「回放」给下游处理器的 reader 适配器。"""

    def __init__(self, head: bytes, reader: asyncio.StreamReader) -> None:
        self._head = head
        self._reader = reader

    async def read(self, n: int = -1) -> bytes:
        if self._head:
            out, self._head = self._head, b""
            return out
        return await self._reader.read(n)

    async def readuntil(self, sep: bytes = b"\r\n\r\n") -> bytes:
        if self._head:
            out, self._head = self._head, b""
            return out
        return await self._reader.readuntil(sep)

    async def readexactly(self, n: int) -> bytes:
        if self._head:
            if len(self._head) >= n:
                out, self._head = self._head[:n], self._head[n:]
                return out
            out, self._head = self._head, b""
            return out + await self._reader.readexactly(n)
        return await self._reader.readexactly(n)


def _is_proxy_request(head: bytes) -> bool:
    """按请求行判断是否代理协议：CONNECT 方法或绝对 URI 目标。"""
    request_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = request_line.split(" ")
    if len(parts) < 2:
        return False
    target = parts[1].lower()
    return parts[0].upper() == "CONNECT" or target.startswith(("http://", "https://"))


class GatewayServer:
    """协议分拣网关：单端口对外，代理协议走 ForwardProxyServer，其余转管理 API。"""

    def __init__(self, proxy: ForwardProxyServer, internal_port: int) -> None:
        self._proxy = proxy
        self._internal_port = internal_port
        self._server: asyncio.AbstractServer | None = None

    @property
    def bound(self) -> str:
        return self._proxy.bound

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._on_conn, *self._proxy.bound.split(":"), limit=HEAD_LIMIT
        )
        logger.info("gateway listening on %s (api on 127.0.0.1:%s)",
                    self._proxy.bound, self._internal_port)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_conn(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"),
                                          timeout=_BACKEND_TIMEOUT)
            if _is_proxy_request(head):
                await self._proxy.handle_client(_ReplayReader(head, reader), writer)
            else:
                await self._to_api(head, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("gateway connection error")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _to_api(self, head: bytes, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        """管理 API：原样转交回环上的 uvicorn，再双向对拷。"""
        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._internal_port),
                timeout=_BACKEND_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            body = b'{"code":50200,"msg":"gateway: api backend unavailable","data":null}'
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
            )
            await writer.drain()
            logger.error("api backend 127.0.0.1:%s unreachable: %s",
                         self._internal_port, exc)
            return
        backend_writer.write(head)
        await asyncio.gather(
            _splice(reader, backend_writer), _splice(backend_reader, writer)
        )


async def _splice(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def _build(settings_path: str | None):
    """按 main.create_app 同款装配：返回 (app, registry, session, settings)。"""
    from .main import create_app

    path = settings_path or _CONFIG_PATH
    settings = load_proxy_settings(path) if os.path.exists(path) else load_proxy_settings()

    route_file = settings.registry.route_file or None
    if route_file and not os.path.isabs(route_file):
        route_file = os.path.abspath(
            os.path.join(os.path.dirname(os.path.abspath(path or _CONFIG_PATH)), route_file)
        )
        if not os.path.exists(route_file):
            route_file = settings.registry.route_file
    registry = Registry(
        route_file=route_file,
        route_url=settings.registry.route_url or None,
        reload_interval=settings.registry.reload_interval,
    )
    session = aiohttp.ClientSession()
    app = create_app(settings, registry=registry, session=session, start_reload=True,
                     start_forward_proxy=False)
    return app, registry, session, settings


async def run(settings_path: str | None = None) -> None:
    import uvicorn

    from .forward_proxy import ForwardProxyServer

    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(message)s")
    app, registry, session, settings = await _build(settings_path)

    fp_cfg = settings.forward_proxy
    if not fp_cfg.enabled:
        raise SystemExit("gateway: forward_proxy.enabled is false; nothing to proxy")
    if fp_cfg.port != settings.service.port:
        raise SystemExit(
            f"gateway: forward_proxy.port ({fp_cfg.port}) != service.port "
            f"({settings.service.port}); 分离端口模式直接用 uvicorn 启动即可，无需网关"
        )

    # 管理 API 的 uvicorn 只听回环
    api_host, api_port = "127.0.0.1", fp_cfg.internal_port
    config = uvicorn.Config(app, host=api_host, port=api_port,
                            log_level=settings.service.log_level.lower())
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    for _ in range(300):
        if server.started:
            break
        await asyncio.sleep(0.02)
    if not server.started:
        raise SystemExit("gateway: internal api server failed to start")

    if registry.route_file or registry.route_url:
        await registry.load()

    proxy = ForwardProxyServer(registry, session, settings)
    proxy.validate()  # 校验路由表可用；9000 由网关绑定，代理本身不另占端口
    gateway = GatewayServer(proxy, api_port)
    await gateway.start()  # 绑定 service.host:service.port（对外唯一端口）
    try:
        await serve_task
    finally:
        await gateway.close()
        await proxy.close()  # 排空在途连接（归还 IP）
        server.should_exit = True
        await registry.close()
        await session.close()


def main() -> None:
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(run(path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
