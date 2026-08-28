"""logging_setup.py 测试：冒烟、格式、级别过滤、文件输出、幂等性。"""
from __future__ import annotations

import logging

import pytest

from ip_pool_common.logging_setup import setup_logging


@pytest.fixture(autouse=True)
def _clean_logging():
    """每个测试前重置日志状态，测试后仅移除 setup_logging 添加的 handler。"""
    import ip_pool_common.logging_setup as ls

    root = logging.getLogger()

    def _ours():
        return [
            h
            for h in root.handlers
            if isinstance(getattr(h, "formatter", None), ls._ServiceFormatter)
        ]

    for handler in _ours():
        root.removeHandler(handler)
        if isinstance(handler, logging.FileHandler):
            handler.close()
    ls._configured = False
    yield
    for handler in _ours():
        root.removeHandler(handler)
        if isinstance(handler, logging.FileHandler):
            handler.close()
    ls._configured = False


def test_setup_logging_smoke(capsys):
    setup_logging("my-service", level="DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("mod.x").info("hello world")
    captured = capsys.readouterr()
    assert "my-service" in captured.err
    assert "INFO" in captured.err
    assert "mod.x" in captured.err
    assert "hello world" in captured.err


def test_setup_logging_level_filter(capsys):
    setup_logging("svc", level="WARNING")
    logging.getLogger("mod").debug("should not appear")
    logging.getLogger("mod").warning("should appear")
    captured = capsys.readouterr()
    assert "should not appear" not in captured.err
    assert "should appear" in captured.err


def test_setup_logging_file_output(tmp_path):
    log_file = tmp_path / "app.log"
    setup_logging("svc", log_file=str(log_file))
    logging.getLogger("mod.y").error("boom happened")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "svc" in text
    assert "ERROR" in text
    assert "mod.y" in text
    assert "boom happened" in text


def test_setup_logging_custom_fmt(capsys):
    setup_logging("svc", fmt="CUSTOM %(levelname)s %(service)s %(message)s")
    logging.getLogger("mod").warning("warn msg")
    captured = capsys.readouterr()
    assert captured.err.startswith("CUSTOM")
    assert "warn msg" in captured.err


def test_setup_logging_no_duplicate_handlers(capsys):
    setup_logging("svc-a")
    handlers_before = list(logging.getLogger().handlers)
    setup_logging("svc-b")
    assert list(logging.getLogger().handlers) == handlers_before


def test_setup_logging_file_idempotent(tmp_path):
    log_file = tmp_path / "app.log"
    target = str(log_file)

    def _ours():
        return [
            h
            for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler)
            and getattr(h, "baseFilename", "") == target
        ]

    setup_logging("svc", log_file=target)
    file_handlers_1 = _ours()
    setup_logging("svc", log_file=target)
    file_handlers_2 = _ours()
    assert len(file_handlers_1) == 1
    assert len(file_handlers_2) == 1