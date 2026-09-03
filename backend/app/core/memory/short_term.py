# app/core/memory/short_term.py
# 短期记忆存储：short_term_memory 表，多行记录——1 条 summary（早期对话压缩摘要）+ 最近几轮对话原文（每条一条）。
# 目的：让「短期记忆 = 摘要 + 最近几轮原文」自包含、边界清晰（role=summary 区分摘要），跨轮持久，供上下文注入与「记忆」页查询。
import asyncio
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """短期记忆：一个会话存「1 条 summary + 最近几轮对话原文」，多行记录，按 id 排序（summary 最前）。

    replace_records 全量替换该会话的短期记忆（清空 + 重写），保证边界每次切分后持久、清晰。
    完整历史正文由 ToolResultStore 落盘到 _history.txt，供 read_file 读回细节。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        # 建表（IF NOT EXISTS 保证幂等）；每条记录存 role（summary/user/assistant）+ content
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS short_term_memory(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    role TEXT,
                    content TEXT,
                    created_at TEXT
                )
            """)

    async def get_records(self, conversation_id):
        """返回该会话短期记忆的 [{role, content}, ...] 列表（按 id 升序，summary 在最前）；无则空列表。"""

        def r():
            with self._conn() as c:
                rows = c.execute(
                    "SELECT role, content FROM short_term_memory WHERE conversation_id=? ORDER BY id",
                    (conversation_id,)).fetchall()
            return [{"role": x["role"], "content": x["content"]} for x in rows]

        return await asyncio.to_thread(r)

    async def replace_records(self, conversation_id, records):
        """全量替换该会话的短期记忆：先清空旧记录，再按序写入新 records（[{role, content}, ...]）。

        records 顺序即最终展示顺序：summary 排最前，随后是最近几轮对话原文。
        """

        def r():
            with self._conn() as c:
                c.execute("DELETE FROM short_term_memory WHERE conversation_id=?", (conversation_id,))
                now = datetime.now().isoformat()
                c.executemany(
                    "INSERT INTO short_term_memory(conversation_id, role, content, created_at) VALUES(?,?,?,?)",
                    [(conversation_id, rec["role"], rec["content"], now) for rec in records])

        await asyncio.to_thread(r)
        logger.info("[记忆:short_term] 更新短期记忆：%s（%d 条记录）", conversation_id, len(records))

    async def clear_conversation(self, conversation_id):
        """删除单会话的短期记忆（删除会话时清理）。"""

        def r():
            with self._conn() as c:
                return c.execute("DELETE FROM short_term_memory WHERE conversation_id=?",
                                 (conversation_id,)).rowcount

        deleted = await asyncio.to_thread(r)
        logger.info("[记忆:short_term] 清理会话短期记忆：%s（%d 条）", conversation_id, deleted)
