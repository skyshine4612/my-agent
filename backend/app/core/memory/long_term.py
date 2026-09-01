# app/core/memory/long_term.py
# 长期记忆存储：long_term_memory 表，跨会话持久偏好，按 user_id 隔离，超容量按 importance 淘汰。
import asyncio
import sqlite3
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# 注入 long_term_hint 时单条 fact 的字符上限，防止历史偏好把提示词撑爆
FACT_HINT_CHAR_LIMIT = 100


class LongTermMemory:
    """长期记忆：存储从对话中提炼出的偏好/事实（long_term_memory），超过容量上限时按 importance 淘汰。"""

    def __init__(self, db_path, max_facts=100):
        # 先建表，再保存容量上限
        self.db_path = db_path
        self.max_facts = max_facts
        self._init()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        # 建 long_term_memory 表（IF NOT EXISTS 保证幂等）
        with self._conn() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS long_term_memory(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, fact TEXT, importance REAL, created_at TEXT)
            """)

    async def add_facts(self, user_id, facts):
        """批量写入多条事实，并在写入后执行容量淘汰：超限时删除 importance 最低的条目。"""

        def r():
            with self._conn() as c:
                for f in facts:
                    c.execute("INSERT INTO long_term_memory(user_id,fact,importance,created_at) VALUES(?,?,?,?)",
                              (user_id, f["fact"], f.get("importance", 0.5), datetime.now().isoformat()))
                # 容量淘汰：仅当「当前用户自己」的 facts 超过 max_facts 时，删除该用户 importance 最低（importance 相同时最早创建）的多余条目。
                # 三重 user_id 过滤实现按用户隔离淘汰，绝不触碰其他用户的数据、不挤占他人配额。
                cur = c.execute(
                    "DELETE FROM long_term_memory WHERE user_id=? AND id IN (SELECT id FROM long_term_memory WHERE user_id=? ORDER BY importance ASC, created_at ASC LIMIT max(0,(SELECT COUNT(*) FROM long_term_memory WHERE user_id=?)-?))",
                    (user_id, user_id, user_id, self.max_facts))
                return len(facts), cur.rowcount

        inserted, deleted = await asyncio.to_thread(r)
        if deleted:
            logger.info("[记忆:long_term] 写入 %d 条长期记忆，容量淘汰 %d 条（importance 最低）", inserted, deleted)
        else:
            logger.info("[记忆:long_term] 写入 %d 条长期记忆", inserted)

    async def get_all(self, user_id):
        """返回某用户的全部事实（按 importance 降序，fact 原样不截断，供语义去重匹配）。"""

        def r():
            with self._conn() as c:
                return [dict(x) for x in c.execute(
                    "SELECT fact,importance FROM long_term_memory WHERE user_id=? ORDER BY importance DESC",
                    (user_id,))]

        return await asyncio.to_thread(r)

    async def recall(self, user_id, top_n=20):
        """按 importance 降序取前 top_n 条事实，每条 fact 截断到 FACT_HINT_CHAR_LIMIT 字符，返回 [{"fact","importance"}]。

        与 get_all 的区别：recall 供组装 long_term_hint 使用，有数量上限 + 单条截断，避免提示词被历史偏好撑爆；
        无硬阈值，importance 仅用于 add_facts 内的容量淘汰与这里的排序。
        """

        def r():
            with self._conn() as c:
                rows = c.execute(
                    "SELECT fact,importance FROM long_term_memory WHERE user_id=? ORDER BY importance DESC LIMIT ?",
                    (user_id, top_n)).fetchall()
            # 单条 fact 截断：只保留前 FACT_HINT_CHAR_LIMIT 字符，控制 long_term_hint 体积
            return [{"fact": x["fact"][:FACT_HINT_CHAR_LIMIT], "importance": x["importance"]} for x in rows]

        result = await asyncio.to_thread(r)
        logger.info("[记忆:long_term] 召回长期记忆 %d 条（top_n=%d）", len(result), top_n)
        return result

    async def update_importance(self, user_id, fact, importance):
        """更新某条已存在事实的 importance（语义去重时提升其权重而非新增重复条目）。"""

        def r():
            with self._conn() as c:
                c.execute("UPDATE long_term_memory SET importance=? WHERE user_id=? AND fact=?",
                          (importance, user_id, fact))

        await asyncio.to_thread(r)
        logger.info("[记忆:long_term] 语义去重：提升已有 fact importance=%.2f（%.50s）", importance, fact)
