# app/core/memory/short_term.py
# 短期记忆存储：short_term_memory 表，喂 LLM 的跨轮短期记忆的权威源。
# 直接存结构化消息序列（user / assistant(含 tool_calls) / tool / summary），跨轮持久，
# 供上下文注入与「记忆」页查询；messages 表只做前端展示，不参与这里。
import asyncio
import logging
from datetime import datetime

from app.core.memory.db import connect

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """短期记忆：喂 LLM 的跨轮上下文的权威存储，多行记录按 id 排序。

    role 取值：summary（累积摘要）/ user / assistant / tool。
    assistant 带 tool_calls 时 content 存 JSON {"tool_calls":[...]}；tool 消息用 tool_call_id 列配对。
    完整历史正文由 ToolResultStore 落盘 _history.txt，供 read_file 读回细节。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._init()

    def _conn(self):
        return connect(self.db_path)

    def _init(self):
        # 建表（IF NOT EXISTS 幂等）；tool_call_id 仅 tool 消息用，其余为 NULL
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS short_term_memory(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT,
                    role TEXT,
                    content TEXT,
                    tool_call_id TEXT,
                    created_at TEXT
                )
            """)

    async def get_records(self, conversation_id):
        """返回该会话短期记忆的 [{role, content, tool_call_id}, ...]（按 id 升序，summary 最前）；无则空列表。"""

        def r():
            with self._conn() as c:
                rows = c.execute(
                    "SELECT role, content, tool_call_id FROM short_term_memory WHERE conversation_id=? ORDER BY id",
                    (conversation_id,)).fetchall()
            return [{"role": x["role"], "content": x["content"], "tool_call_id": x["tool_call_id"]} for x in rows]

        return await asyncio.to_thread(r)

    async def append_records(self, conversation_id, records):
        """追加消息到短期记忆（不清空旧记录）；records 为 [{role, content, tool_call_id}, ...]。"""

        def r():
            with self._conn() as c:
                now = datetime.now().isoformat()
                c.executemany(
                    "INSERT INTO short_term_memory(conversation_id, role, content, tool_call_id, created_at) VALUES(?,?,?,?,?)",
                    [(conversation_id, rec["role"], rec["content"], rec.get("tool_call_id"), now) for rec in records])

        await asyncio.to_thread(r)
        logger.info("[记忆:short_term] 追加短期记忆：%s（+%d 条）", conversation_id, len(records))

    async def replace_records(self, conversation_id, records):
        """全量替换该会话的短期记忆（清空 + 重写），压缩后写回 summary + 剩余消息时用。"""

        def r():
            with self._conn() as c:
                c.execute("DELETE FROM short_term_memory WHERE conversation_id=?", (conversation_id,))
                now = datetime.now().isoformat()
                c.executemany(
                    "INSERT INTO short_term_memory(conversation_id, role, content, tool_call_id, created_at) VALUES(?,?,?,?,?)",
                    [(conversation_id, rec["role"], rec["content"], rec.get("tool_call_id"), now) for rec in records])

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
