"""本地 mock 站点：任何路径/方法均返回 200。

E2E 确定性场景中作为二级池 site_test 的目标站点（经转发代理访问），
仅用于 mock 辅助场景（E2E-05/09），与真实公网站点互不干扰。
"""
from __future__ import annotations

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Mock Site")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "HEAD"])
async def any_path(path: str, request: Request):
    return JSONResponse(
        {"ok": True, "path": path or "/", "method": request.method},
        status_code=200,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9100, log_level="info")