# app/core/memory/db.py
# SQLite 连接统一入口：会话历史 / 短期记忆 / 长期记忆共享同一 db_path，
# 统一在这里设置 WAL 模式 + busy_timeout，避免并发写时 database is locked。
import sqlite3


def connect(db_path):
    """返回配置好的 SQLite 连接：row_factory=Row + WAL + busy_timeout。

    WAL 是数据库文件级属性（设置一次即持久），busy_timeout 是连接级（每次连接都设）；
    两者幂等，每次连接执行无害。
    """
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c
