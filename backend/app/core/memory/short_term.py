# app/core/memory/short_term.py
# 短期记忆存储：short_term_memory 表，任务周期内已查工具摘要，按 conversation_id 隔离，UPSERT 去重。
# 目的：Agent 每轮 ReAct 前取回「本会话已查清单」注入工作记忆，防止反复重查同一工具。
import asyncio
import json
import sqlite3
from datetime import datetime


class ShortTermMemory:
    """短期记忆：存储会话内已查工具的摘要（tool_name + args + summary），按 conversation_id 隔离。

    唯一约束 (conversation_id, tool_name, args) 保证「重复查同一工具+参数」时覆盖旧行而非新增，
    使记录数 = 会话内查过的不同工具组合数，有界、不随轮次膨胀。
    """

    def __init__(self, db_path):
        self.db_path = db_path
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        # 建表 + 去重唯一索引（IF NOT EXISTS 保证幂等）
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS short_term_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT,
                tool_name TEXT,
                args TEXT,
                summary TEXT,
                created_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_short_term_dedup
                ON short_term_memory(conversation_id, tool_name, args);
            """)

    @staticmethod
    def _normalize_args(args) -> str:
        """把工具入参规范化为稳定字符串，作为去重 key（相同参数生成相同字符串）。"""
        if isinstance(args, dict):
            return json.dumps(args, sort_keys=True, ensure_ascii=False)
        return json.dumps(args, ensure_ascii=False) if args is not None else "{}"

    async def write(self, conversation_id, tool_name, args, summary):
        """写入（UPSERT）一条已查摘要：同 (conversation_id, tool_name, args) 时覆盖旧行 summary 与 created_at。

        重复查询同一工具不新增记录，从源头控制短期记忆体积。
        """
        args_json = self._normalize_args(args)

        def r():
            with self._conn() as c:
                c.execute("""
                    INSERT INTO short_term_memory(conversation_id, tool_name, args, summary, created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(conversation_id, tool_name, args)
                    DO UPDATE SET summary=excluded.summary, created_at=excluded.created_at
                """, (conversation_id, tool_name, args_json, summary, datetime.now().isoformat()))

        await asyncio.to_thread(r)

    async def get_all(self, conversation_id):
        """按 created_at 升序返回该会话全部已查摘要，供拼 short_term_hint（极简清单）。"""

        def r():
            with self._conn() as c:
                rows = c.execute(
                    "SELECT tool_name, args, summary, created_at FROM short_term_memory WHERE conversation_id=? ORDER BY created_at",
                    (conversation_id,)).fetchall()
            return [dict(x) for x in rows]

        return await asyncio.to_thread(r)

    async def clear_conversation(self, conversation_id):
        """删除单会话的短期记忆（删主会话或子会话任务结束时清理）。

        子会话使用独立 uuid 作为 conversation_id，任务结束时各自清理，不与主会话产生前缀关联，
        故清理只需按 conversation_id 精确匹配即可。
        """

        def r():
            with self._conn() as c:
                c.execute("DELETE FROM short_term_memory WHERE conversation_id=?", (conversation_id,))

        await asyncio.to_thread(r)
