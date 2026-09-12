# tests/test_logging.py
# 日志接线的回归测试：uvicorn 的异常栈必须能落进 logs/app.log。
import logging

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


def test_uvicorn_loggers_feed_root():
    """uvicorn / uvicorn.error 必须上行到 root，否则未捕获异常栈只进 stderr。"""
    _replay_uvicorn_logger_setup()
    setup_logging()

    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        assert lg.handlers == [], f"{name} 自带 handler 会把日志截在半路"
        assert lg.propagate is True, f"{name} 关掉了 propagate，异常栈到不了 root"


def test_uvicorn_error_record_actually_reaches_root():
    """行为断言：uvicorn.error 上打的记录要真的走到 root 的 handler。"""
    _replay_uvicorn_logger_setup()
    setup_logging()

    seen = []
    probe = logging.Handler()
    probe.emit = seen.append
    root = logging.getLogger()
    root.addHandler(probe)
    try:
        logging.getLogger("uvicorn.error").error("探针异常")
    finally:
        root.removeHandler(probe)

    assert [r.getMessage() for r in seen] == ["探针异常"]


def test_access_log_stays_off_the_file():
    """access 日志（每个请求一行）不接进 root，避免把日志文件刷成请求流水。"""
    _replay_uvicorn_logger_setup()
    setup_logging()

    assert logging.getLogger("uvicorn.access").propagate is False
