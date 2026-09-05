"""代理层配置：ProxySettings（pydantic-settings）。

YAML 为基底、环境变量覆盖，字段默认值与 proxy/项目策划书.md「配置设计」一致。
"""
from __future__ import annotations

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from ip_pool_common.config import load_settings


class ServiceConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9000
    log_level: str = "INFO"


class RegistryConfig(BaseModel):
    route_file: str = "config/proxy_routes.yaml"
    route_url: str = ""
    reload_interval: float = 60.0


class Level1Config(BaseModel):
    base_url: str = "http://127.0.0.1:8000"
    timeout: float = 5.0


class DispatchConfig(BaseModel):
    timeout: float = 10.0


class AuthConfig(BaseModel):
    """账号认证：接口调用方凭据 → 定向二级池。

    - 带 Basic 凭据：校验通过后强制使用账号绑定的池（``accounts`` 表）；
    - 无凭据：只允许访问默认池（``default_site``，空 = 路由表第一个站点）；
    - 凭据错误回 401，访问了非本人/非默认池回 403。
    """

    default_site: str = ""  # 无凭据调用方走的默认池；空 = 路由表第一个站点
    db_path: str = ""  # 账号 SQLite 路径；空 = <proxy>/data/accounts.db


class TunnelConfig(BaseModel):
    """隧道代理入口（标准正向代理，一个端口 + 凭据，对齐行业惯例）。

    - 下游填 ``http://user:pass@host:port`` 即可使用整个池：
      HTTP 绝对 URI 转发、HTTPS 走 CONNECT 隧道（支持流式）；
    - 凭据查 ``accounts`` 表定池（与接口鉴权同一套账号）；
      无凭据走默认池（auth.default_site）；凭据非法回 407；
    - 每请求 acquire 一个出口 IP，坏 IP 自动换（max_attempts），用完 release；
      池空轮询等待 acquire_max_wait 秒后回 502。
    """

    enabled: bool = False
    host: str | None = None  # None → 随 service.host
    port: int = 9001  # 独立代理入口端口，与 9000 管理 API 互不干扰
    max_attempts: int = 4  # 单请求最多换几个出口 IP
    connect_timeout: float = 10.0  # 连上游出口 IP 的超时秒数
    upstream_timeout: float = 15.0  # 上游 CONNECT 响应/二级池接口超时秒数
    acquire_max_wait: float = 30.0  # 池空时最长等待秒数，超时回 502
    acquire_interval: float = 2.0  # 池空轮询间隔秒数


class ProxySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXY_", extra="ignore")

    service: ServiceConfig = ServiceConfig()
    registry: RegistryConfig = RegistryConfig()
    level1: Level1Config = Level1Config()
    dispatch: DispatchConfig = DispatchConfig()
    auth: AuthConfig = AuthConfig()
    tunnel: TunnelConfig = TunnelConfig()


def load_proxy_settings(path: str | None = None) -> ProxySettings:
    if path is None:
        return ProxySettings()
    return load_settings(ProxySettings, path, "PROXY_")