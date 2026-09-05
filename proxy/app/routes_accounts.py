"""账号管理路由：注册 / 列表 / 删除（内网管理接口，同二级池管理口待遇，不设管理鉴权）。

- 注册时校验 ``assigned_site`` 必须在当前路由表中（防手滑绑到不存在的池）；
- 列表不返回任何密码材料。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ip_pool_common.api import ErrorCode, err, ok

from .accounts import AccountNotFoundError, DuplicateAccountError

router = APIRouter(prefix="/api/v1", tags=["accounts"])


class AccountCreate(BaseModel):
    username: str
    password: str
    assigned_site: str  # 绑定的二级池站点名


@router.post("/accounts")
async def create_account(body: AccountCreate, request: Request) -> JSONResponse:
    username = body.username.strip()
    assigned_site = body.assigned_site.strip()
    if not username or not body.password or not assigned_site:
        return JSONResponse(
            status_code=400,
            content=err(ErrorCode.PARAM_ERROR,
                        "username/password/assigned_site are all required"),
        )
    if request.app.state.registry.get(assigned_site) is None:
        return JSONResponse(
            status_code=404,
            content=err(ErrorCode.NOT_FOUND,
                        f"site not configured: {assigned_site}"),
        )
    try:
        account = request.app.state.accounts.create(username, body.password,
                                                    assigned_site)
    except DuplicateAccountError:
        return JSONResponse(
            status_code=400,
            content=err(ErrorCode.PARAM_ERROR, f"account exists: {username}"),
        )
    return ok({
        "username": account.username,
        "assigned_site": account.assigned_site,
        "created_at": account.created_at,
    })


@router.get("/accounts")
async def list_accounts(request: Request) -> JSONResponse:
    accounts = [
        {"username": a.username, "assigned_site": a.assigned_site,
         "created_at": a.created_at}
        for a in request.app.state.accounts.list()
    ]
    return ok({"accounts": accounts, "total": len(accounts)})


@router.delete("/accounts/{username}")
async def delete_account(username: str, request: Request) -> JSONResponse:
    try:
        request.app.state.accounts.delete(username)
    except AccountNotFoundError:
        return JSONResponse(
            status_code=404,
            content=err(ErrorCode.NOT_FOUND, f"account not found: {username}"),
        )
    return ok({"username": username, "deleted": True})
