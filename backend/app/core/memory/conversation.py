# app/core/memory/conversation.py
# 会话历史存储：基于标准库 sqlite3，维护 conversations（会话元信息）与 messages（消息记录）两张表。
# 所有阻塞的数据库操作通过 asyncio.to_thread 丢到线程池执行，避免阻塞事件循环。
import asyncio
import sqlite3
import uuid
from datetime import datetime


class ConversationStore:
    """会话历史：维护多轮对话的会话元信息（conversations）与消息记录（messages），按 user_id 隔离。"""

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
        # 用 executescript 一次性建好 conversations / messages 两张表（IF NOT EXISTS 保证幂等）
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, user_id TEXT, title TEXT, created_at TEXT);
            CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, content TEXT, created_at TEXT);
            """)

    async def create_conversation(self, user_id, title="新会话"):
        """创建一个新会话，返回会话 id。"""
        cid = uuid.uuid4().hex

        def r():
            with self._conn() as c:
                c.execute("INSERT INTO conversations VALUES(?,?,?,?)",
                          (cid, user_id, title, datetime.now().isoformat()))

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

        与 get_history 的读路径校验同源，供写路径（如 chat_stream 落库前）与记忆查询 API 做归属校验：
        客户端传入的 conv_id 若不属于当前 user_id，应拒绝访问，防止跨用户注入/读取。
        """

        def r():
            with self._conn() as c:
                return c.execute("SELECT 1 FROM conversations WHERE id=? AND user_id=?",
                                 (conv_id, user_id)).fetchone() is not None

        return await asyncio.to_thread(r)

    async def list_conversations(self, user_id):
        """列出某用户的所有会话（按创建时间倒序），按 user_id 过滤实现用户隔离。"""

        def r():
            with self._conn() as c:
                return [dict(x) for x in c.execute(
                    "SELECT id,title,created_at FROM conversations WHERE user_id=? ORDER BY created_at DESC",
                    (user_id,))]

        return await asyncio.to_thread(r)

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
