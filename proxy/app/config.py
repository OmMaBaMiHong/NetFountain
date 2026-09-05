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


class ForwardProxyConfig(BaseModel):
    """内置标准正向代理：把站点二级池的租赁 IP 直接暴露为标准正向代理端口。

    - sub2api 等只认「host:port 正向代理」，填本端口即可使用代理池；
    - ``site`` 为空时取路由表第一个站点；每次请求 acquire 一个 IP，
      失败自动换 IP 重试，用完 release；池空轮询等待；
    - ``auth_user``/``auth_pass`` 同时非空时启用 Basic 代理认证。
    """

    enabled: bool = False
    host: str | None = None  # None → 随 service.host
    port: int = 9001  # 设为 service.port（9000）即单端口网关模式（须用 python -m app.gateway 启动）
    internal_port: int = 19000  # 网关模式下管理 API 实际监听的回环端口（仅 127.0.0.1，外部不可见）
    site: str = ""  # 从哪个站点的二级池租 IP；空 = 路由表第一个
    max_attempts: int = 4  # 单请求最多换几个 IP
    connect_timeout: float = 10.0  # 连上游代理（免费 IP）超时秒数
    upstream_timeout: float = 15.0  # 上游 CONNECT 响应/二级池接口超时秒数
    acquire_max_wait: float = 30.0  # 池空时最长等待秒数，超时回 502
    acquire_interval: float = 2.0  # 池空轮询间隔秒数
    auth_user: str = ""
    auth_pass: str = ""


class ProxySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PROXY_", extra="ignore")

    service: ServiceConfig = ServiceConfig()
    registry: RegistryConfig = RegistryConfig()
    level1: Level1Config = Level1Config()
    dispatch: DispatchConfig = DispatchConfig()
    forward_proxy: ForwardProxyConfig = ForwardProxyConfig()


def load_proxy_settings(path: str | None = None) -> ProxySettings:
    if path is None:
        return ProxySettings()
    return load_settings(ProxySettings, path, "PROXY_")