"""冒烟测试：验证配置模块可导入，且用示例配置文件实例化成功。"""
from app.config import ProxySettings, load_proxy_settings


def test_config_import_and_load():
    settings = load_proxy_settings("config/proxy_routes.yaml")
    assert isinstance(settings, ProxySettings)
    assert settings.service.port == 9000
    assert settings.registry.route_file == "config/proxy_routes.yaml"
    assert settings.registry.reload_interval == 60.0
    assert settings.dispatch.timeout == 10.0