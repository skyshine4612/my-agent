# app/core/logging.py
"""应用日志配置：统一控制台 + 滚动文件输出，供各模块记录运行过程。

日志级别约定：
- INFO：正常运行过程（请求进入、agent 生成、工具调用、SSE 事件）
- WARNING：可恢复的异常/兜底（工具调用失败、解析失败）
- ERROR：未预期的错误
"""
import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logging():
    """配置根 logger：控制台 + 滚动文件（logs/app.log），统一格式。

    格式：时间 | 级别 | 模块名 | 消息。文件按 10MB × 5 份滚动，避免无限增长。
    """
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 单文件 10MB
        backupCount=5,  # 保留 5 份历史
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 避免重复添加 handler（uvicorn reload 时会多次触发 setup）
    if not root.handlers:
        root.addHandler(console)
        root.addHandler(file_handler)
    # HTTP 客户端每发一个请求就打一条 INFO（一次入库几十条 embedding），只保留失败
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # uvicorn 默认把日志写到自己接的 stderr，不往 root 传 —— 未捕获的异常栈因此
    # 进不了 logs/app.log。这里把它改成走 root，异常才会落进文件。
    # 时序没问题：import app.main（触发本函数）发生在 uvicorn 配置完自己的日志之后。
    # access 请求日志不接，只进终端，免得把日志文件刷成请求流水。
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
