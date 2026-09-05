"""accounts 测试：账号注册/列表/删除 + 租还端点 Basic 鉴权与定向池路由。

覆盖：
- 注册（缺参 400 / 重复 400 / 未知站点 404）、列表不含密码材料、删除（不存在 404）；
- 带凭据：绑定池内放行、越池 403、密码错/账号不存在 401、删除后凭据失效；
- 无凭据：默认池放行、非默认池 403 提示注册；
- 只读端点（status）不鉴权；非法 Authorization 头回 401。

上游二级池统一用 aio_mock 桩（经 mock_session 注入 dispatcher），桩窗口
随测试全程开启（aio_mock 是 mock_session 的依赖夹具）。
"""
from __future__ import annotations

import pytest

from app.config import AuthConfig, ProxySettings
from app.main import create_app

ACQUIRE_OK = {"code": 0, "msg": "ok",
              "data": {"id": 1, "ip": "10.0.0.1", "port": 8001}}


@pytest.fixture
async def client(tmp_path, registry, mock_session, running_app):
    """代理层应用 + httpx 客户端；默认池 site_a，账号库在临时目录。"""
    settings = ProxySettings(
        auth=AuthConfig(default_site="site_a",
                        db_path=str(tmp_path / "accounts.db"))
    )
    app = create_app(settings, registry=registry, session=mock_session,
                     start_reload=False)
    async with running_app(app) as c:
        yield c


async def _register(client, username="u1", password="pw",
                    assigned_site="site_b"):
    return await client.post(
        "/api/v1/accounts",
        json={"username": username, "password": password,
              "assigned_site": assigned_site},
    )


async def test_register_list_delete(client):
    r = await _register(client)
    assert r.status_code == 200
    assert r.json()["data"]["assigned_site"] == "site_b"

    r = await client.get("/api/v1/accounts")
    accounts = r.json()["data"]["accounts"]
    assert [a["username"] for a in accounts] == ["u1"]
    assert all("password" not in a and "password_hash" not in a
               for a in accounts)

    r = await client.delete("/api/v1/accounts/u1")
    assert r.json()["data"]["deleted"] is True
    r = await client.delete("/api/v1/accounts/u1")
    assert r.status_code == 404
    assert r.json()["code"] == 40400


async def test_register_validation(client):
    r = await _register(client)  # 先成功注册 u1，供重复注册用例使用
    assert r.status_code == 200

    r = await _register(client, username=" ")
    assert r.status_code == 400

    r = await _register(client, username="u2", password="")
    assert r.status_code == 400

    r = await _register(client, username="u2", assigned_site="nope")
    assert r.status_code == 404
    assert r.json()["code"] == 40400

    r = await _register(client)
    assert r.status_code == 400
    assert "exists" in r.json()["msg"]


async def test_acquire_with_account_enforces_bound_pool(client, aio_mock):
    await _register(client)  # u1 → site_b
    aio_mock.post("http://127.0.0.1:8002/api/v1/ips/acquire",
                  payload=ACQUIRE_OK)

    # 绑定池内：放行并透传到 site_b 的二级池
    r = await client.post("/api/v1/site_b/ips/acquire", auth=("u1", "pw"))
    assert r.status_code == 200
    assert r.json()["code"] == 0

    # 越池：绑定池外的站点一律 403
    r = await client.post("/api/v1/site_a/ips/acquire", auth=("u1", "pw"))
    assert r.status_code == 403
    assert r.json()["code"] == 40300

    # 密码错 / 账号不存在：401
    r = await client.post("/api/v1/site_b/ips/acquire", auth=("u1", "wrong"))
    assert r.status_code == 401
    assert r.json()["code"] == 40101
    r = await client.post("/api/v1/site_b/ips/acquire", auth=("ghost", "pw"))
    assert r.status_code == 401

    # 删除账号后原凭据立即失效
    r = await client.delete("/api/v1/accounts/u1")
    assert r.json()["code"] == 0
    r = await client.post("/api/v1/site_b/ips/acquire", auth=("u1", "pw"))
    assert r.status_code == 401


async def test_acquire_without_account_limited_to_default_pool(client, aio_mock):
    aio_mock.post("http://127.0.0.1:8001/api/v1/ips/acquire",
                  payload=ACQUIRE_OK)
    r = await client.post("/api/v1/site_a/ips/acquire")
    assert r.status_code == 200
    assert r.json()["code"] == 0

    r = await client.post("/api/v1/site_b/ips/acquire")
    assert r.status_code == 403
    assert r.json()["code"] == 40300
    assert "register" in r.json()["msg"]


async def test_readonly_status_open_without_auth(client, aio_mock):
    aio_mock.get("http://127.0.0.1:8002/api/v1/status",
                 payload={"code": 0, "msg": "ok", "data": {"site": "site_b"}})
    r = await client.get("/api/v1/site_b/status")
    assert r.status_code == 200
    assert r.json()["data"]["site"] == "site_b"


async def test_malformed_authorization_header_401(client):
    r = await client.post("/api/v1/site_b/ips/acquire",
                          headers={"Authorization": "Bearer xyz"})
    assert r.status_code == 401
    assert r.json()["code"] == 40101

    r = await client.post("/api/v1/site_b/ips/acquire",
                          headers={"Authorization": "Basic %%%"})
    assert r.status_code == 401
