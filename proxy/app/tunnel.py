"""隧道代理入口：一个端口 + 凭据的标准正向代理（对齐行业惯例）。

对外行为与商业隧道代理一致：
- 下游把 ``http://user:pass@本机:tunnel.port`` 填进代理配置即可使用整个池；
- HTTP 绝对 URI 请求经池内 IP 原样转发，HTTPS 走 CONNECT 隧道（建联后
  双向盲转发，内容加密不可见，天然支持流式）；
- ``Proxy-Authorization: Basic`` 凭据查 ``accounts`` 账号表（与 9000 的
  账号定向池同一套）→ 该账号绑定的池；无凭据 → 默认池（auth.default_site）；
  凭据缺失格式错/密码错 → 407；
- 每个请求/每条连接从池里 acquire 一个出口 IP，失败自动换下一个重试
  （max_attempts），用完 release 归还；池空按间隔轮询等待，超时回 502。

实现边界：独立端口只讲代理协议这一件事——不做协议分拣、没有内部端口、
不动 9000 与 uvicorn 启动方式。与二级池的交互为普通 HTTP 接口调用
（acquire/release，与 9000 透传同一套信封）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from typing import Optional, Tuple

import aiohttp

from .accounts import AccountStore
from .config import ProxySettings, TunnelConfig
from .registry import Registry

logger = logging.getLogger(__name__)

HEAD_LIMIT = 64 * 1024

# 逐跳头：不在客户端与上游代理之间转发
HOP_HEADERS = {
    "proxy-authorization", "proxy-connection", "connection",
    "keep-alive", "te", "trailers", "upgrade",
}


class TunnelServer:
    """随代理层启停的隧道代理入口（同一事件循环，经 HTTP 租还二级池 IP）。"""

    def __init__(self, registry: Registry, session: aiohttp.ClientSession,
                 accounts: AccountStore, settings: ProxySettings) -> None:
        self._registry = registry
        self._session = session
        self._accounts = accounts
        self._cfg: TunnelConfig = settings.tunnel
        self._auth_cfg = settings.auth
        self._host = self._cfg.host or settings.service.host
        self._port = self._cfg.port
        self._server: Optional[asyncio.AbstractServer] = None
        self._conns: set[asyncio.Task] = set()

    @property
    def bound(self) -> str:
        return f"{self._host}:{self._port}"

    # ------------------------------------------------------------------
    # 池解析与租/还
    # ------------------------------------------------------------------

    def _default_site(self) -> Optional[str]:
        """无凭据调用方允许的池：auth.default_site，空则取路由表第一个。"""
        configured = self._auth_cfg.default_site
        if configured:
            return configured
        sites = self._registry.sites()
        return sites[0].name if sites else None

    def validate(self) -> str:
        """启动前置校验：默认池必须可解析（路由表非空）。"""
        site = self._default_site()
        if site is None:
            raise RuntimeError("tunnel: route table is empty, no pool to lease from")
        return site

    async def start(self) -> None:
        base_site = self.validate()
        self._server = await asyncio.start_server(
            self._on_conn, self._host, self._port, limit=HEAD_LIMIT
        )
        logger.info("tunnel proxy listening on %s (default pool: %s)",
                    self.bound, base_site)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        pending = [t for t in self._conns if not t.done()]
        if pending:  # 在途请求把 IP 归还做完再放行（最多等 5 秒）
            await asyncio.wait(pending, timeout=5.0)
        logger.info("tunnel proxy closed (%s)", self.bound)

    def _base_url(self, site: str) -> Optional[str]:
        route = self._registry.get(site)
        if route is None:
            logger.warning("tunnel: pool site %r not in route table", site)
            return None
        return route.base_url.rstrip("/")

    async def _acquire(self, site: str) -> Optional[dict]:
        """从 site 池租一个 IP，返回 {'id','ip','port'}；池空/不可达轮询等待。"""
        base = self._base_url(site)
        if base is None:
            return None
        timeout = aiohttp.ClientTimeout(total=self._cfg.upstream_timeout)
        deadline = time.monotonic() + self._cfg.acquire_max_wait
        while True:
            try:
                async with self._session.post(
                    f"{base}/api/v1/ips/acquire", json={}, timeout=timeout
                ) as resp:
                    payload = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("tunnel acquire failed on %s: %s", base, exc)
                payload = {}
            data = payload.get("data") or {}
            if payload.get("code") == 0 and data.get("ip") and data.get("port"):
                return {"id": data.get("id"), "ip": data["ip"],
                        "port": int(data["port"])}
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(self._cfg.acquire_interval)

    async def _release(self, site: str, entry_id) -> None:
        if entry_id is None:
            return
        base = self._base_url(site)
        if base is None:
            return
        try:
            async with self._session.post(
                f"{base}/api/v1/ips/{entry_id}/release", json={},
                timeout=aiohttp.ClientTimeout(total=self._cfg.upstream_timeout),
            ) as resp:
                await resp.read()
                logger.debug("tunnel release %s ok", entry_id)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("tunnel release %s failed on %s: %s", entry_id, base, exc)
        except Exception:  # noqa: BLE001
            logger.exception("tunnel release %s unexpected error", entry_id)

    # ------------------------------------------------------------------
    # 连接收发基础件
    # ------------------------------------------------------------------

    async def _on_conn(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        """登记在途连接任务，保证 close() 能等它们全部收尾（归还 IP）后再关。"""
        task = asyncio.current_task()
        assert task is not None
        self._conns.add(task)
        try:
            await self._handle_client(reader, writer)
        finally:
            self._conns.discard(task)

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

    async def _reject(self, writer: asyncio.StreamWriter, status: int,
                      reason: str, extra_headers: str = "") -> None:
        body = b'{"error":"%s"}' % reason.encode()
        writer.write(
            (
                f"HTTP/1.1 {status} {reason}\r\n"
                f"{extra_headers}"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1") + body
        )
        await writer.drain()

    def _resolve_site(self, headers: list) -> Tuple[Optional[str], bool]:
        """凭据 → 账号绑定池；无凭据 → 默认池。

        返回 ``(site, ok)``；``ok=False`` 表示凭据非法（调用方回 407）。
        """
        auth = self._header(headers, "Proxy-Authorization") or ""
        scheme, _, token = auth.partition(" ")
        if not auth.strip():
            return self._default_site(), True
        if scheme.lower() != "basic" or not token.strip():
            return None, False
        try:
            decoded = base64.b64decode(token.strip()).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None, False
        username, _, password = decoded.partition(":")
        account = self._accounts.verify(username, password)
        if account is None:
            return None, False
        return account.assigned_site, True

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
            site, authed = self._resolve_site(headers)
            if not authed or site is None:
                await self._reject(
                    writer, 407, "proxy authentication required",
                    'Proxy-Authenticate: Basic realm="netfountain"\r\n',
                )
                return
            if method == "CONNECT":
                await self._handle_connect(reader, writer, target, site)
            else:
                await self._handle_plain(reader, writer, method, target,
                                         headers, site)
        except (ConnectionError, asyncio.IncompleteReadError, RuntimeError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("tunnel proxy client error")
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _connect_via(self, entry: dict, hostport: str
                           ) -> Optional[Tuple[asyncio.StreamReader, asyncio.StreamWriter]]:
        """经上游出口 IP 建立到目标的 CONNECT 隧道，失败返回 None。"""
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(entry["ip"], entry["port"]),
                timeout=self._cfg.connect_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("tunnel upstream %s:%s dial failed: %s",
                        entry["ip"], entry["port"], exc)
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
            logger.info("tunnel upstream %s:%s CONNECT refused: %s",
                        entry["ip"], entry["port"], status_line)
            up_writer.close()
            return None
        return up_reader, up_writer

    async def _handle_connect(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter, target: str,
                              site: str) -> None:
        entry = await self._acquire(site)
        if entry is None:
            await self._reject(writer, 502, "pool empty")
            return
        released = False

        async def rel() -> None:
            nonlocal released
            if not released:
                await self._release(site, entry["id"])
                released = True

        tunnel = None
        try:
            for attempt in range(1, self._cfg.max_attempts + 1):
                tunnel = await self._connect_via(entry, target)
                if tunnel:
                    break
                await rel()
                logger.info("tunnel CONNECT %s attempt %s/%s failed via %s:%s, rotating",
                            target, attempt, self._cfg.max_attempts,
                            entry["ip"], entry["port"])
                if attempt < self._cfg.max_attempts:
                    entry = await self._acquire(site)
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
            logger.info("tunnel %s teardown complete (lease %s)",
                        target, entry["id"])
        finally:
            await rel()

    async def _handle_plain(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter, method: str,
                            target: str, headers: list, site: str) -> None:
        if target.startswith("/"):  # origin-form 补全为绝对 URI
            host = self._header(headers, "Host") or ""
            target = f"http://{host}{target}"
        body = b""
        clen = self._header(headers, "Content-Length")
        if clen and int(clen) > 0:
            body = await reader.readexactly(int(clen))

        entry = await self._acquire(site)
        if entry is None:
            await self._reject(writer, 502, "pool empty")
            return
        released = False

        async def rel() -> None:
            nonlocal released
            if not released:
                await self._release(site, entry["id"])
                released = True

        try:
            for attempt in range(1, self._cfg.max_attempts + 1):
                try:
                    up_reader, up_writer = await asyncio.wait_for(
                        asyncio.open_connection(entry["ip"], entry["port"]),
                        timeout=self._cfg.connect_timeout,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.info("tunnel upstream %s:%s dial failed: %s",
                                entry["ip"], entry["port"], exc)
                    await rel()
                    if attempt >= self._cfg.max_attempts:
                        break
                    entry = await self._acquire(site)
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
                    entry = await self._acquire(site)
                    if entry is None:
                        break
                    released = False
                    continue

                status_line = first.split(b"\r\n", 1)[0].decode("latin-1")
                logger.info("tunnel plain %s %s via %s:%s (lease %s) -> %s",
                            method, target, entry["ip"], entry["port"],
                            entry["id"], status_line)
                writer.write(first)
                await writer.drain()
                await self._pipe(up_reader, writer)
                return
            await self._reject(writer, 502, "all upstream proxies failed")
        finally:
            await rel()
