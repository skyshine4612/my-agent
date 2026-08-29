# app/core/memory.py
# 三层记忆 + 会话存储模块：基于标准库 sqlite3 实现持久化，
# 提供 ConversationStore（会话历史）、TaskMemory（长任务状态）、LongTermMemory（长期 facts）。
# 所有阻塞的数据库操作通过 asyncio.to_thread 丢到线程池执行，避免阻塞事件循环。
import sqlite3
import uuid
import asyncio
from datetime import datetime


class _Base:
    """建表基类：三个存储类共享同一套 SQLite 建表逻辑（共 5 张表），并统一持有数据库路径。"""

    def __init__(self, db_path):
        # 保存数据库路径，并立即初始化建表（表已存在时跳过）
        self.db_path = db_path
        self._init()

    def _conn(self):
        # 建立连接并设置 row_factory，使查询结果可按列名访问（如 row["role"]）
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init(self):
        # 用 executescript 一次性建好全部 5 张表（IF NOT EXISTS 保证幂等）
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, content TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS task_runs(id TEXT PRIMARY KEY, conversation_id TEXT, plan_json TEXT, status TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS subtask_results(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, subtask_id TEXT, agent TEXT, input TEXT, output TEXT);
            CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, fact TEXT, importance REAL, created_at TEXT);
            """)


class ConversationStore(_Base):
    """会话存储：维护多轮对话的会话元信息（conversations）与消息记录（messages）。"""

    async def create_conversation(self, user_id, title="新会话"):
        """创建一个新会话，返回会话 id。"""
        cid = uuid.uuid4().hex

        def r():
            with self._conn() as c:
                c.execute("INSERT INTO conversations VALUES(?,?,?,?)", (cid, user_id, title, datetime.now().isoformat()))

        await asyncio.to_thread(r)
        return cid

    async def add_message(self, conv_id, role, content):
        """向指定会话追加一条消息（role 为 user/assistant/tool 等）。"""
        def r():
            with self._conn() as c:
                c.execute("INSERT INTO messages(conv_id,role,content,created_at) VALUES(?,?,?,?)",
                          (conv_id, role, content, datetime.now().isoformat()))

        await asyncio.to_thread(r)

    async def get_history(self, user_id, conv_id):
        """按时间顺序返回指定会话的历史消息；先校验会话归属该用户，归属不对返回空列表。"""
        def r():
            with self._conn() as c:
                # 用户隔离校验：会话必须同时匹配 id 与 user_id，防止跨用户读取
                ok = c.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)).fetchone()
                if not ok:
                    return []
                rows = c.execute("SELECT role,content FROM messages WHERE conv_id=? ORDER BY id", (conv_id,)).fetchall()
            return [{"role": x["role"], "content": x["content"]} for x in rows]

        return await asyncio.to_thread(r)

    async def belongs_to(self, user_id, conv_id):
        """校验指定会话是否归属该用户，返回 bool。

        与 get_history 的读路径校验同源，供写路径（如 plan_stream 落库前）做归属校验：
        客户端传入的 conv_id 若不属于当前 user_id，写路径应拒绝写入，防止跨用户注入消息。
        """
        def r():
            with self._conn() as c:
                return c.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)).fetchone() is not None

        return await asyncio.to_thread(r)

    def list_conversations(self, user_id):
        """列出某用户的所有会话（按创建时间倒序），按 user_id 过滤实现用户隔离。"""
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT id,title,created_at FROM conversations WHERE user_id=? ORDER BY created_at DESC", (user_id,))]

    async def update_title(self, conv_id, title):
        """更新会话标题（首次用户消息时用消息摘要作为标题）。"""
        def r():
            with self._conn() as c:
                c.execute("UPDATE conversations SET title=? WHERE id=?", (title, conv_id))
        await asyncio.to_thread(r)

    async def delete_conversation(self, user_id, conv_id):
        """删除会话及其消息；先校验归属，归属不对返回 False（防止跨用户删除）。"""
        def r():
            with self._conn() as c:
                ok = c.execute("SELECT id FROM conversations WHERE id=? AND user_id=?", (conv_id, user_id)).fetchone()
                if not ok:
                    return False
                c.execute("DELETE FROM messages WHERE conv_id=?", (conv_id,))
                c.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
                return True
        return await asyncio.to_thread(r)


class TaskMemory(_Base):
    """任务记忆：存储长任务的执行计划（task_runs）与各子任务的中间结果（subtask_results），支持结果传递与基本恢复。"""

    async def create_task(self, conversation_id, plan):
        """创建一个任务运行记录，保存任务计划（序列化为 JSON），返回任务 id。"""
        tid = uuid.uuid4().hex

        def r():
            with self._conn() as c:
                c.execute("INSERT INTO task_runs VALUES(?,?,?,?,?)",
                          (tid, conversation_id, __import__("json").dumps(plan, ensure_ascii=False), "running", datetime.now().isoformat()))

        await asyncio.to_thread(r)
        return tid

    async def save_subtask_result(self, task_id, subtask_id, agent, input_, output):
        """保存某子任务的执行结果（记录执行 agent、输入与输出）。"""
        def r():
            with self._conn() as c:
                c.execute("INSERT INTO subtask_results(task_id,subtask_id,agent,input,output) VALUES(?,?,?,?,?)",
                          (task_id, subtask_id, agent, input_, output))

        await asyncio.to_thread(r)

    async def get_results(self, task_id):
        """返回某任务所有子任务的输出，映射为 {subtask_id: output}，用于结果传递。"""
        def r():
            with self._conn() as c:
                rows = c.execute("SELECT subtask_id,output FROM subtask_results WHERE task_id=?", (task_id,)).fetchall()
            return {x["subtask_id"]: x["output"] for x in rows}

        return await asyncio.to_thread(r)


class LongTermMemory(_Base):
    """长期记忆：存储从对话中提炼出的偏好/事实（facts），超过容量上限时按 importance 淘汰。"""

    def __init__(self, db_path, max_facts=100):
        # 先调用基类完成建表，再保存容量上限
        super().__init__(db_path)
        self.max_facts = max_facts

    async def add_facts(self, user_id, facts):
        """批量写入多条事实，并在写入后执行容量淘汰：超限时删除 importance 最低的条目。"""
        def r():
            with self._conn() as c:
                for f in facts:
                    c.execute("INSERT INTO facts(user_id,fact,importance,created_at) VALUES(?,?,?,?)",
                              (user_id, f["fact"], f.get("importance", 0.5), datetime.now().isoformat()))
                # 容量淘汰：仅当「当前用户自己」的 facts 超过 max_facts 时，删除该用户 importance 最低（importance 相同时最早创建）的多余条目。
                # 三重 user_id 过滤实现按用户隔离淘汰，绝不触碰其他用户的数据、不挤占他人配额。
                c.execute("DELETE FROM facts WHERE user_id=? AND id IN (SELECT id FROM facts WHERE user_id=? ORDER BY importance ASC, created_at ASC LIMIT max(0,(SELECT COUNT(*) FROM facts WHERE user_id=?)-?))",
                          (user_id, user_id, user_id, self.max_facts))

        await asyncio.to_thread(r)

    async def get_all(self, user_id):
        """返回某用户的全部事实（按 importance 降序），按 user_id 过滤实现用户隔离。"""
        def r():
            with self._conn() as c:
                return [dict(x) for x in c.execute("SELECT fact,importance FROM facts WHERE user_id=? ORDER BY importance DESC", (user_id,))]

        return await asyncio.to_thread(r)
