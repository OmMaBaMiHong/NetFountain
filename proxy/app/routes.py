"""HTTP 路由：/health + 8 个按站点透传端点。

统一响应 {code,msg,data}：透传端点把上游二级池响应原样返回（不缓存、
不加工任何 IP/租赁数据）；站点未配置返回 40400；上游不可达/超时返回 50200。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ip_pool_common.api import ErrorCode, err, ok

from .dispatcher import SiteNotFound, UpstreamError

router = APIRouter(prefix="/api/v1", tags=["proxy"])


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
        return JSONResponse(
            status_code=404,
            content=err(ErrorCode.NOT_FOUND, "site not configured"),
        )
    except UpstreamError:
        return JSONResponse(
            status_code=502,
            content=err(ErrorCode.UPSTREAM_ERROR, "upstream error"),
        )
    return JSONResponse(status_code=status, content=body)


@router.get("/health")
async def health(request: Request):
    registry = request.app.state.registry
    sites = [
        {"name": r.name, "base_url": r.base_url, "target_url": r.target_url}
        for r in registry.sites()
    ]
    return ok({"status": "ok", "sites": sites})


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
    return await _forward(request, site, "POST")


@router.post("/{site}/ips/{id_}/release")
async def release(site: str, id_: int, request: Request):
    return await _forward(request, site, "POST")


@router.delete("/{site}/ips/{id_}")
async def delete(site: str, id_: int, request: Request):
    return await _forward(request, site, "DELETE")


@router.post("/{site}/ips/release-all")
async def release_all(site: str, request: Request):
    return await _forward(request, site, "POST")
