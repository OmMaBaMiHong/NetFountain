"""config.py 测试：YAML 读取、环境变量覆盖、必填校验、默认值填充。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from pydantic_settings import BaseSettings

from ip_pool_common.config import (
    _deep_merge,
    _read_env_overrides,
    load_settings,
    load_yaml,
)


class _TestSettings(BaseSettings):
    model_config = {"env_prefix": "TEST_"}

    name: str = "default"
    count: int = 1
    required: str


def test_load_yaml_valid(tmp_yaml):
    path = tmp_yaml("name: hello\ncount: 3\n")
    assert load_yaml(path) == {"name": "hello", "count": 3}


def test_load_yaml_empty(tmp_yaml):
    path = tmp_yaml("")
    assert load_yaml(path) == {}


def test_load_yaml_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_yaml("no_such_file.yaml")


def test_load_yaml_non_mapping(tmp_yaml):
    path = tmp_yaml("- a\n- b\n")
    with pytest.raises(ValueError):
        load_yaml(path)


def test_load_settings_env_overrides_yaml(tmp_yaml, monkeypatch):
    path = tmp_yaml("name: from_yaml\ncount: 2\nrequired: ok\n")
    monkeypatch.setenv("TEST_NAME", "from_env")
    settings = load_settings(_TestSettings, path, "TEST_")
    assert settings.name == "from_env"
    assert settings.count == 2
    assert settings.required == "ok"


def test_load_settings_missing_required(tmp_yaml, monkeypatch):
    path = tmp_yaml("name: x\ncount: 1\n")
    for key in ("TEST_NAME", "TEST_COUNT", "TEST_REQUIRED"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        load_settings(_TestSettings, path, "TEST_")


def test_load_settings_defaults_filled(tmp_yaml, monkeypatch):
    path = tmp_yaml("required: hello\n")
    for key in ("TEST_NAME", "TEST_COUNT", "TEST_REQUIRED"):
        monkeypatch.delenv(key, raising=False)
    settings = load_settings(_TestSettings, path, "TEST_")
    assert settings.name == "default"
    assert settings.count == 1
    assert settings.required == "hello"


def test_load_settings_no_path_no_prefix(monkeypatch):
    monkeypatch.setenv("TEST_REQUIRED", "x")
    settings = load_settings(_TestSettings)
    assert settings.name == "default"
    assert settings.count == 1
    assert settings.required == "x"


def test_load_settings_nested_env_path(monkeypatch):
    class _Nested(BaseSettings):
        service: dict = {}

    monkeypatch.setenv("APP_SERVICE__PORT", "8080")
    settings = load_settings(_Nested, env_prefix="APP_")
    assert settings.service == {"port": "8080"}


def test_read_env_overrides(monkeypatch):
    monkeypatch.setenv("CFG_NAME", "x")
    monkeypatch.setenv("CFG_SERVICE__PORT", "8080")
    monkeypatch.setenv("OTHER_VAR", "y")
    monkeypatch.setenv("CFG", "bare")
    monkeypatch.setenv("CFG_", "empty-after-prefix")
    result = _read_env_overrides("CFG_")
    assert result == {"name": "x", "service": {"port": "8080"}}


def test_read_env_overrides_prefix_case_insensitive(monkeypatch):
    monkeypatch.setenv("cfg_alpha", "1")
    monkeypatch.setenv("cfg_beta__gamma", "2")
    result = _read_env_overrides("CFG_")
    assert result == {"alpha": "1", "beta": {"gamma": "2"}}


def test_deep_merge_scalar_and_nested():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    _deep_merge(base, {"a": 99, "b": {"d": 4}, "e": 5})
    assert base == {"a": 99, "b": {"c": 2, "d": 4}, "e": 5}


def test_deep_merge_scalar_overwrites_nested():
    base = {"a": {"x": 1}}
    _deep_merge(base, {"a": 5})
    assert base == {"a": 5}