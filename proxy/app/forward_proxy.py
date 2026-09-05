"""内置标准正向代理：把各站点二级池的租赁 IP 直接暴露为标准 HTTP 正向代理端口。

与二级池的交互就是普通 HTTP 接口调用（与 dispatcher 同款）：
每次请求向站点二级池 ``POST /api/v1/ips/acquire`` 租一个 IP，失败换 IP 重试，
用完 ``POST /api/v1/ips/{id}/release`` 归还；池空按间隔轮询等待，超时回 502。

- 对外讲标准正向代理协议：HTTP 绝对 URI 请求经上游 IP 原样转发；
  HTTPS 走 CONNECT 隧道（建联后双向盲转发，天然支持 SSE 流式）；
- 复用应用级 aiohttp 会话调二级池；TCP 盲转发用纯 asyncio，无新增依赖；
- 由 lifespan 启停（``forward_proxy.enabled`` 控制），与 FastAPI 同一事件循环。
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Optional, Tuple

import aiohttp

from .config import ForwardProxyConfig, ProxySettings
from .registry import Registry

logger = logging.getLogger(__name__)

HEAD_LIMIT = 64 * 1024

# 逐跳头：不在客户端与上游代理之间转发
HOP_HEADERS = {
    "proxy-authorization", "proxy-connection", "connection",
    "keep-alive", "te", "trailers", "upgrade",
}


class ForwardProxyServer:
    """随代理层启停的正向代理服务器（同一事件循环，经 HTTP 租还二级池 IP）。"""

    def __init__(self, registry: Registry, session: aiohttp.ClientSession,
                 settings: ProxySettings) -> None:
        self._registry = registry
        self._session = session
        self._cfg: ForwardProxyConfig = settings.forward_proxy
        self._host = self._cfg.host or settings.service.host
        self._port = self._cfg.port
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: set[asyncio.Task] = set()

    @property
    def bound(self) -> str:
        return f"{self._host}:{self._port}"

    def _base_url(self) -> str:
        """解析目标站点二级池地址；未配置站点时取路由表第一个。"""
        site = self._cfg.site
        route = self._registry.get(site) if site else None
        if route is None:
            sites = self._registry.sites()
            if not sites:
                raise RuntimeError("forward proxy: route table is empty, no site to lease from")
            route = sites[0]
        return route.base_url.rstrip("/")

    def validate(self) -> str:
        """启动前置校验：路由表里有站点可租；返回二级池基地址。"""
        return self._base_url()

    async def start(self) -> None:
        base = self.validate()  # 启动时先校验路由表可用，失败即启动失败
        self._server = await asyncio.start_server(
            self._on_conn, self._host, self._port, limit=HEAD_LIMIT
        )
        logger.info("forward proxy listening on %s (level2: %s)", self.bound, base)

    async def _on_conn(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        """登记在途连接任务，保证 close() 能等它们全部收尾（归还 IP）后再关会话。"""
        task = asyncio.current_task()
        assert task is not None
        self._conns.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            self._conns.discard(task)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        pending = [t for t in self._conns if not t.done()]
        if pending:  # 在途请求把 IP 归还做完再放行（最多等 5 秒）
            await asyncio.wait(pending, timeout=5.0)
        logger.info("forward proxy closed (%s)", self.bound)

    # ------------------------------------------------------------------
    # 二级池租/还（HTTP 接口，与 dispatcher 同款信封 {code,msg,data}）
    # ------------------------------------------------------------------

    async def _acquire(self) -> Optional[dict]:
        """租一个 IP，返回 {'id','ip','port'}；池空/不可达返回 None（内部轮询等待）。"""
        base = self._base_url()
        timeout = aiohttp.ClientTimeout(total=self._cfg.upstream_timeout)
        deadline = time.monotonic() + self._cfg.acquire_max_wait
        while True:
            try:
                async with self._session.post(
                    f"{base}/api/v1/ips/acquire", json={}, timeout=timeout
                ) as resp:
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("acquire failed on %s: %s", base, exc)
                payload = {}
            data = payload.get("data") or {}
            if payload.get("code") == 0 and data.get("ip") and data.get("port"):
                return {"id": data.get("id"), "ip": data["ip"], "port": int(data["port"])}
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._cfg.acquire_interval)

    async def _release(self, entry_id) -> None:
        if entry_id is None:
            return
        base = self._base_url()
        try:
            async with self._session.post(
                f"{base}/api/v1/ips/{entry_id}/release", json={},
                timeout=aiohttp.ClientTimeout(total=self._cfg.upstream_timeout),
            ) as resp:
                await resp.read()
                logger.debug("release %s ok", entry_id)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("release %s failed on %s: %s", entry_id, base, exc)
        except Exception:  # noqa: BLE001
            logger.exception("release %s unexpected error on %s", entry_id, base)

    # ------------------------------------------------------------------
    # 基础件
    # ------------------------------------------------------------------

    async def _read_head(self, reader: asyncio.StreamReader) -> Optional[bytes]:
        try:
            return await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=self._cfg.upstream_timeout
            )
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                asyncio.LimitOverrunError, ConnectionError):
            return None

    @staticmethod
    def _parse_head(raw: bytes) -> Tuple[str, list]:
        lines = raw.split(b"\r\n")
        return lines[0].decode("latin-1"), [l.decode("latin-1") for l in lines[1:] if l]

    @staticmethod
    def _header(headers: list, name: str) -> Optional[str]:
        prefix = name.lower() + ":"
        for h in headers:
            if h.lower().startswith(prefix):
                return h[len(prefix):].strip()
        return None

    async def _reject(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = b'{"error":"%s"}' % reason.encode()
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1") + body
        )
        await writer.drain()

    def _authorized(self, headers: list) -> bool:
        if not self._cfg.auth_user:
            return True
        got = self._header(headers, "Proxy-Authorization") or ""
        expect = "Basic " + base64.b64encode(
            f"{self._cfg.auth_user}:{self._cfg.auth_pass}".encode()
        ).decode()
        return got == expect

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:  # noqa: BLE001 隧道单侧断开属正常
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 请求处理
    # ------------------------------------------------------------------

    async def handle_client(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> None:
        """处理一条代理协议连接（网关模式由 gateway 分拣后调用）。"""
        await self._handle_client(reader, writer)

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        try:
            raw = await self._read_head(reader)
            if raw is None:
                return
            head, headers = self._parse_head(raw)
            parts = head.split(" ")
            if len(parts) < 3:
                return
            method, target = parts[0], parts[1]
            if not self._authorized(headers):
                await self._reject(writer, 407, "proxy authentication required")
                return
            if method == "CONNECT":
                await self._handle_connect(reader, writer, target)
            else:
                await self._handle_plain(reader, writer, method, target, headers)
        except (ConnectionError, asyncio.IncompleteReadError, RuntimeError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("forward proxy client error")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _connect_via(self, entry: dict, hostport: str
                           ) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """经上游免费 IP 建立到目标的 CONNECT 隧道，失败返回 None。"""
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(entry["ip"], entry["port"]),
                timeout=self._cfg.connect_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("proxy %s:%s dial failed: %s", entry["ip"], entry["port"], exc)
            return None
        up_writer.write(
            f"CONNECT {hostport} HTTP/1.1\r\nHost: {hostport}\r\n"
            "Proxy-Connection: close\r\n\r\n".encode("latin-1")
        )
        await up_writer.drain()
        raw = await self._read_head(up_reader)
        if raw is None:
            up_writer.close()
            return None
        status_line, _ = self._parse_head(raw)
        if " 200" not in status_line:
            logger.info("proxy %s:%s CONNECT refused: %s", entry["ip"], entry["port"], status_line)
            up_writer.close()
            return None
        return up_reader, up_writer

    async def _handle_connect(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter, target: str) -> None:
        entry = await self._acquire()
        if entry is None:
            await self._reject(writer, 502, "pool empty")
            return
        released = False

        async def rel() -> None:
            nonlocal released
            if not released:
                await self._release(entry["id"])
                released = True

        tunnel = None
        try:
            for attempt in range(1, self._cfg.max_attempts + 1):
                tunnel = await self._connect_via(entry, target)
                if tunnel:
                    break
                await rel()
                logger.info("CONNECT %s attempt %s/%s failed via %s:%s, rotating",
                            target, attempt, self._cfg.max_attempts,
                            entry["ip"], entry["port"])
                if attempt < self._cfg.max_attempts:
                    entry = await self._acquire()
                    if entry is None:
                        break
                    released = False
            if tunnel is None:
                await self._reject(writer, 502, "all upstream proxies failed")
                return
            up_reader, up_writer = tunnel
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            logger.info("tunnel %s via %s:%s (lease %s)",
                        target, entry["ip"], entry["port"], entry["id"])
            await asyncio.gather(self._pipe(reader, up_writer),
                                 self._pipe(up_reader, writer))
            logger.info("tunnel %s teardown complete (lease %s)", target, entry["id"])
        finally:
            await rel()

    async def _handle_plain(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter, method: str,
                            target: str, headers: list) -> None:
        if target.startswith("/"):  # origin-form 补全为绝对 URI
            host = self._header(headers, "Host") or ""
            target = f"http://{host}{target}"
        body = b""
        clen = self._header(headers, "Content-Length")
        if clen and int(clen) > 0:
            body = await reader.readexactly(int(clen))

        entry = await self._acquire()
        if entry is None:
            await self._reject(writer, 502, "pool empty")
            return
        released = False

        async def rel() -> None:
            nonlocal released
            if not released:
                await self._release(entry["id"])
                released = True

        try:
            for attempt in range(1, self._cfg.max_attempts + 1):
                try:
                    up_reader, up_writer = await asyncio.wait_for(
                        asyncio.open_connection(entry["ip"], entry["port"]),
                        timeout=self._cfg.connect_timeout,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info("proxy %s:%s dial failed: %s", entry["ip"], entry["port"], exc)
                    await rel()
                    if attempt >= self._cfg.max_attempts:
                        break
                    entry = await self._acquire()
                    if entry is None:
                        break
                    released = False
                    continue

                lines = [f"{method} {target} HTTP/1.1"]
                for h in headers:
                    if h.split(":", 1)[0].strip().lower() not in HOP_HEADERS:
                        lines.append(h)
                lines.append("Connection: close")
                payload = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
                up_writer.write(payload)
                await up_writer.drain()

                first = await up_reader.read(65536)
                if not first:  # 上游 IP 秒断：换一个重试
                    up_writer.close()
                    await rel()
                    if attempt >= self._cfg.max_attempts:
                        break
                    entry = await self._acquire()
                    if entry is None:
                        break
                    released = False
                    continue

                status_line = first.split(b"\r\n", 1)[0].decode("latin-1")
                logger.info("plain %s %s via %s:%s (lease %s) -> %s",
                            method, target, entry["ip"], entry["port"], entry["id"],
                            status_line)
                writer.write(first)
                await writer.drain()
                await self._pipe(up_reader, writer)
                return
            await self._reject(writer, 502, "all upstream proxies failed")
        finally:
            await rel()
