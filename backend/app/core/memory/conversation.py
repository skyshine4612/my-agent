# app/core/memory/conversation.py
# 会话历史存储：conversations（会话元信息）与 messages（消息记录）两张表。
# 每条查询都跑在 rls_conn(user_id) 的事务里，显式的 user_id 条件与数据库 RLS 策略构成双保险。
import logging
import uuid

from app.core.db import rls_conn

logger = logging.getLogger(__name__)


class ConversationStore:
    """会话历史：维护多轮对话的会话元信息（conversations）与消息记录（messages），按 user_id 隔离。"""

    async def create_conversation(self, user_id, title="新会话"):
        """创建一个新会话，返回会话 id。"""
        cid = uuid.uuid4().hex
        async with rls_conn(user_id) as conn:
            await conn.execute(
                "insert into conversations(id, user_id, title) values($1, $2, $3)",
                cid, user_id, title)
        return cid

    async def add_message(self, user_id, conv_id, role, content):
        """向指定会话追加一条消息（role 为 user/assistant/tool 等）。"""

        async with rls_conn(user_id) as conn:
            await conn.execute(
                "insert into messages(conv_id, role, content) values($1, $2, $3)",
                conv_id, role, content)

    async def get_history(self, user_id, conv_id):
        """按时间顺序返回指定会话的历史消息（前端对话历史展示用）；会话不属于该用户时返回空列表。

        每条返回 {"id","role","content"}。
        """
        async with rls_conn(user_id) as conn:
            # 用户隔离校验：会话必须同时匹配 id 与 user_id，防止跨用户读取
            ok = await conn.fetchrow(
                "select 1 from conversations where id=$1 and user_id=$2", conv_id, user_id)
            if ok is None:
                return []
            rows = await conn.fetch(
                "select id, role, content from messages where conv_id=$1 order by id", conv_id)
        return [{"id": r["id"], "role": r["role"], "content": r["content"]} for r in rows]

    async def belongs_to(self, user_id, conv_id):
        """校验指定会话是否归属该用户，返回 bool。

        与 get_history 的读路径校验同源，供写路径（如 chat_stream 落库前）与记忆查询 API 做归属校验：
        客户端传入的 conv_id 若不属于当前 user_id，应拒绝访问，防止跨用户注入/读取。
        """
        async with rls_conn(user_id) as conn:
            ok = await conn.fetchrow(
                "select 1 from conversations where id=$1 and user_id=$2", conv_id, user_id)
        return ok is not None

    async def list_conversations(self, user_id):
        """列出某用户的所有会话（按创建时间倒序），按 user_id 过滤实现用户隔离。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select id, title, created_at from conversations where user_id=$1 "
                "order by created_at desc", user_id)
        return [dict(r) for r in rows]

    async def update_title(self, user_id, conv_id, title):
        """更新会话标题（首次用户消息时用消息摘要作为标题）。"""
        async with rls_conn(user_id) as conn:
            await conn.execute(
                "update conversations set title=$1 where id=$2 and user_id=$3",
                title, conv_id, user_id)

    async def delete_conversation(self, user_id, conv_id):
        """删除会话，返回是否删除成功（归属不对返回 False）。

        messages / short_term_memory 由外键 on delete cascade 自动跟着删，
        因此这里只需一条 DELETE，不必再逐表清理。
        """
        async with rls_conn(user_id) as conn:
            row = await conn.fetchrow(
                "delete from conversations where id=$1 and user_id=$2 returning id",
                conv_id, user_id)
        return row is not None
