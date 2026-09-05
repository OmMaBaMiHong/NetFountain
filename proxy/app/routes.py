"""HTTP 路由：/health + 9 个按站点透传端点 + 租还类端点账号鉴权。

统一响应 {code,msg,data}：透传端点把上游二级池响应原样返回（不缓存、
不加工任何 IP/租赁数据）；站点未配置返回 40400；上游不可达/超时返回 50200。
代理层只统计自身活动（调用/来源/站点转发/自身错误 40400、50200），不统计
任何二级池业务信息（如上游业务码）。/health 额外实时聚合一级池与各站点
二级池的 ``/status`` 信息（``pools``），下游不可达时该条目标记为 error。

鉴权规则（只约束租还类端点；status/count/ips/health 等只读端点开放）：
- 带 ``Authorization: Basic`` 凭据：校验通过强制走账号绑定的池，
  请求站点与绑定池不符回 403；凭据错误回 401；
- 无凭据：只允许访问默认池（``auth.default_site``，空 = 路由表第一个），
  访问其它池回 403 提示注册。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from datetime import datetime, timezone

import aiohttp
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ip_pool_common.api import ErrorCode, err, ok

from .dispatcher import SiteNotFound, UpstreamError

router = APIRouter(prefix="/api/v1", tags=["proxy"])

# 本层自有错误码（账号鉴权，仅代理层使用）
AUTH_FAILED = 40101  # 用户名或密码错误 / 凭据格式非法
POOL_FORBIDDEN = 40300  # 请求站点与账号绑定池（或默认池）不符


def _record(request: Request, *, site: str | None = None, error_code: int | None = None) -> None:
    """记录一次代理层调用统计（站点转发 / 错误计数）。"""
    stats = getattr(request.app.state, "stats", None)
    if stats is None:
        return
    if site is not None:
        stats.record_site(site)
    if error_code is not None:
        stats.record_error(error_code)


def _default_site(request: Request) -> str:
    """无凭据调用方允许的池：``auth.default_site``，空则取路由表第一个。"""
    configured = request.app.state.settings.auth.default_site
    if configured:
        return configured
    sites = request.app.state.registry.sites()
    return sites[0].name if sites else ""


def _authorize(request: Request, site: str) -> JSONResponse | None:
    """租还类端点鉴权：通过返回 ``None``，否则返回 401/403 响应。

    - Basic 凭据正确 → 必须访问账号绑定的池；
    - 无凭据 → 只允许访问默认池；
    - 凭据缺失格式错/密码错 → 401（附 ``WWW-Authenticate``）；越池 → 403；
    - 站点不在路由表 → 直接放行，由 dispatcher 按原契约回 40400（鉴权只管真实存在的池）。
    """
    if request.app.state.registry.get(site) is None:
        return None
    header = request.headers.get("authorization", "")
    if header:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "basic" or not token.strip():
            return JSONResponse(
                status_code=401,
                content=err(AUTH_FAILED, "unsupported authorization scheme"),
                headers={"WWW-Authenticate": "Basic"},
            )
        try:
            decoded = base64.b64decode(token.strip()).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return JSONResponse(
                status_code=401,
                content=err(AUTH_FAILED, "malformed basic credentials"),
                headers={"WWW-Authenticate": "Basic"},
            )
        username, _, password = decoded.partition(":")
        account = request.app.state.accounts.verify(username, password)
        if account is None:
            return JSONResponse(
                status_code=401,
                content=err(AUTH_FAILED, "invalid username or password"),
                headers={"WWW-Authenticate": "Basic"},
            )
        if account.assigned_site != site:
            return JSONResponse(
                status_code=403,
                content=err(
                    POOL_FORBIDDEN,
                    f"account {username!r} is bound to pool "
                    f"{account.assigned_site!r}, not {site!r}",
                ),
            )
        return None
    default = _default_site(request)
    if site != default:
        return JSONResponse(
            status_code=403,
            content=err(
                POOL_FORBIDDEN,
                f"unauthenticated access is limited to the default pool "
                f"{default!r}; register via POST /api/v1/accounts",
            ),
        )
    return None


async def _forward(request: Request, site: str, method: str) -> JSONResponse:
    """按站点透传请求到对应二级池，原样返回上游 {code,msg,data}。"""
    dispatcher = request.app.state.dispatcher
    json_body = None
    if method in ("POST", "PUT", "PATCH"):
        raw = await request.body()
        if raw:
            try:
                json_body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                json_body = None
    try:
        status, body = await dispatcher.forward(
            site,
            method,
            request.url.path,
            params=dict(request.query_params),
            json_body=json_body,
        )
    except SiteNotFound:
        _record(request, error_code=int(ErrorCode.NOT_FOUND))
        return JSONResponse(
            status_code=404,
            content=err(ErrorCode.NOT_FOUND, "site not configured"),
        )
    except UpstreamError:
        _record(request, error_code=int(ErrorCode.UPSTREAM_ERROR))
        return JSONResponse(
            status_code=502,
            content=err(ErrorCode.UPSTREAM_ERROR, "upstream error"),
        )
    _record(request, site=site)
    return JSONResponse(status_code=status, content=body)


async def _fetch_pool_status(
    session: aiohttp.ClientSession, base_url: str, timeout: float
) -> dict:
    """GET ``{base_url}/api/v1/status``，返回其 ``data``；失败时返回 ``{"error": ...}``。"""
    url = base_url.rstrip("/") + "/api/v1/status"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                return {"error": f"HTTP {resp.status}"}
            body = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        return {"error": str(exc) or type(exc).__name__}
    data = body.get("data") if isinstance(body, dict) else None
    return data if isinstance(data, dict) else {"error": "no status data"}


@router.get("/health")
async def health(request: Request):
    registry = request.app.state.registry
    session = request.app.state.dispatcher.session
    settings = request.app.state.settings
    routes = registry.sites()

    level1_url = settings.level1.base_url
    level1_status, *site_statuses = await asyncio.gather(
        _fetch_pool_status(session, level1_url, settings.level1.timeout),
        *(
            _fetch_pool_status(session, r.base_url, settings.level1.timeout)
            for r in routes
        ),
    )

    sites = [
        {"name": r.name, "base_url": r.base_url, "target_url": r.target_url}
        for r in routes
    ]
    pool_sites = [
        {"name": r.name, "base_url": r.base_url, "status": status}
        for r, status in zip(routes, site_statuses)
    ]
    started_at = datetime.fromtimestamp(
        request.app.state.start_time, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "status": "ok",
        "started_at": started_at,
        "uptime": round(time.time() - request.app.state.start_time, 3),
        "stats": request.app.state.stats.snapshot(),
        "sites": sites,
        "pools": {
            "level1": {"base_url": level1_url, "status": level1_status},
            "sites": pool_sites,
        },
    }
    return ok(data)


@router.get("/{site}/status")
async def status(site: str, request: Request):
    return await _forward(request, site, "GET")


@router.get("/{site}/count")
async def count(site: str, request: Request):
    return await _forward(request, site, "GET")


@router.get("/{site}/ips")
async def ips(site: str, request: Request):
    return await _forward(request, site, "GET")


@router.post("/{site}/ips/acquire")
async def acquire(site: str, request: Request):
    denied = _authorize(request, site)
    if denied is not None:
        return denied
    return await _forward(request, site, "POST")


@router.post("/{site}/ips/acquire-batch")
async def acquire_batch(site: str, request: Request):
    denied = _authorize(request, site)
    if denied is not None:
        return denied
    return await _forward(request, site, "POST")


@router.post("/{site}/ips/{id_}/release")
async def release(site: str, id_: int, request: Request):
    denied = _authorize(request, site)
    if denied is not None:
        return denied
    return await _forward(request, site, "POST")


@router.delete("/{site}/ips/{id_}")
async def delete(site: str, id_: int, request: Request):
    denied = _authorize(request, site)
    if denied is not None:
        return denied
    return await _forward(request, site, "DELETE")


@router.post("/{site}/ips/release-all")
async def release_all(site: str, request: Request):
    denied = _authorize(request, site)
    if denied is not None:
        return denied
    return await _forward(request, site, "POST")
