"""冒烟测试：验证配置模块可导入，且用示例配置文件实例化成功。"""
from app.config import Level1Settings, load_level1_settings


def test_config_import_and_load():
    settings = load_level1_settings("config/level1_pool.yaml")
    assert isinstance(settings, Level1Settings)
    assert settings.service.port == 8000
    assert settings.provider.pull_count == 10
    assert settings.pool.max_size == 500
    assert settings.test_timeout == 3.0
    assert settings.test_concurrency == 10
    assert settings.ttl_sweep_interval == 5.0