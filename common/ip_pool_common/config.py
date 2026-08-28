"""配置加载：YAML 为基底、环境变量覆盖。

提供 `load_yaml` 与 `load_settings`，供三个业务项目复用。
"""
from __future__ import annotations

import os
from typing import Any

import yaml
from pydantic_settings import BaseSettings


def load_yaml(path: str) -> dict[str, Any]:
    """读取 YAML 文件并返回 dict。文件不存在或格式非法时抛出明确异常。"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"config file must contain a mapping at top level: {path}")
    return data


def load_settings(
    settings_cls: type[BaseSettings],
    path: str | None = None,
    env_prefix: str = "",
) -> BaseSettings:
    """YAML 为基底，环境变量（env_prefix 前缀）可覆盖，实例化为 pydantic-settings 对象。

    环境变量约定：前缀 + 双下划线分隔的嵌套路径，例如前缀 `LEVEL1_`、
    环境变量 `LEVEL1_SERVICE__PORT=8080` 覆盖 `service.port`。
    缺省字段用类默认值填充，缺失必填项会抛出校验异常。
    """
    data: dict[str, Any] = {}
    if path is not None:
        data = load_yaml(path)
    if env_prefix:
        _deep_merge(data, _read_env_overrides(env_prefix))
    return settings_cls(**data)


def _read_env_overrides(prefix: str) -> dict[str, Any]:
    """读取带前缀的环境变量，转为嵌套 dict（`A__B` → `{a: {b: ...}}`）。"""
    prefix = prefix.upper()
    result: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.upper().startswith(prefix):
            continue
        parts = [part.lower() for part in key[len(prefix):].split("__") if part]
        if not parts:
            continue
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """原地合并：键值覆盖，嵌套 dict 递归合并。"""
    for key, value in override.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value