# tests/test_logging.py
# 日志接线的回归测试：uvicorn 的异常栈必须能落进 logs/app.log。
import logging

import pytest
from uvicorn.config import LOGGING_CONFIG

from app.core.logging import setup_logging


def _replay_uvicorn_logger_setup():
    """复刻 uvicorn.run() 里 dictConfig 对这三个 logger 的处置，作为被测起点。

    不直接调 dictConfig：它会连带改 root 与全量现有 logger，污染同一次会话里的其他用例。
    生产里的顺序正是「uvicorn 先 dictConfig → 再 import app.main 触发 setup_logging」。
    """
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        cfg = LOGGING_CONFIG["loggers"].get(name, {})
        lg = logging.getLogger(name)
        lg.handlers = [logging.NullHandler() for _ in cfg.get("handlers", [])]
        lg.propagate = cfg.get("propagate", True)


@pytest.mark.parametrize("name", ["uvicorn", "uvicorn.error"])
def test_uvicorn_loggers_reach_root(name):
    """行为断言：这两个 logger 上打的记录要真的走到 root 的 handler。

    setup_logging 清空它们自带的 handler 并打开 propagate，为的就是未捕获异常栈能落进
    logs/app.log（uvicorn 默认只写自己的 stderr）。这里用探针记录验证「真的到了」，
    而不是断言 handlers / propagate 这两个内部接线属性。
    """
    _replay_uvicorn_logger_setup()
    setup_logging()

    seen = []
    probe = logging.Handler()
    probe.emit = seen.append
    root = logging.getLogger()
    root.addHandler(probe)
    try:
        logging.getLogger(name).error("探针异常")
    finally:
        root.removeHandler(probe)

    assert [r.getMessage() for r in seen] == ["探针异常"]


def test_access_log_stays_off_the_file():
    """access 日志（每个请求一行）不接进 root，避免把日志文件刷成请求流水。"""
    _replay_uvicorn_logger_setup()
    setup_logging()

    assert logging.getLogger("uvicorn.access").propagate is False
