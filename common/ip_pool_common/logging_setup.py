"""日志初始化：统一格式（时间戳/服务名/级别/模块/消息），支持 stdout 与可选文件输出。

可重复调用不产生重复 handler。
"""
from __future__ import annotations

import logging
import os

_DEFAULT_FMT = "%(asctime)s %(levelname)s %(service)s [%(name)s] %(message)s"
_DEFAULT_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured = False


class _ServiceFormatter(logging.Formatter):
    """在每条日志记录上注入服务名，供格式串 %(service)s 引用。"""

    def __init__(self, fmt: str, service_name: str) -> None:
        super().__init__(fmt=fmt, datefmt=_DEFAULT_DATE_FMT)
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        record.service = self._service_name
        return super().format(record)


def setup_logging(
    service_name: str,
    level: str = "INFO",
    fmt: str | None = None,
    log_file: str | None = None,
) -> None:
    """初始化结构化日志。

    统一格式：``时间戳 级别 服务名 [模块] 消息``。支持标准输出与可选文件输出；
    可重复调用：不会重复添加已有 handler，重复调用仅更新根日志级别。
    """
    global _configured

    root = logging.getLogger()
    root.setLevel(level.upper())

    fmt = fmt or _DEFAULT_FMT
    formatter = _ServiceFormatter(fmt, service_name)

    if not _configured:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
        _configured = True

    if log_file:
        target = os.path.abspath(log_file)
        has_file = any(
            isinstance(h, logging.FileHandler)
            and os.path.abspath(getattr(h, "baseFilename", "")) == target
            for h in root.handlers
        )
        if not has_file:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)