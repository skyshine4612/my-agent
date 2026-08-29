# app/core/logging.py
"""应用日志配置：统一控制台 + 滚动文件输出，供各模块记录运行过程。

日志级别约定：
- INFO：正常运行过程（请求进入、Planner 拆解、子任务执行、工具调用、SSE 事件）
- WARNING：可恢复的异常/兜底（信息不全澄清、工具调用失败、解析失败）
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
        maxBytes=10 * 1024 * 1024,   # 单文件 10MB
        backupCount=5,               # 保留 5 份历史
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
