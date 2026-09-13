# app/core/memory/short_term.py
# 短期记忆存储：short_term_memory 表，喂 LLM 的跨轮短期记忆的权威源。
# 直接存结构化消息序列（user / assistant(含 tool_calls) / tool / summary），跨轮持久，
# 供上下文注入与「记忆」页查询；messages 表只做前端展示，不参与这里。
import logging

from app.core.db import rls_conn

logger = logging.getLogger(__name__)


class ShortTermMemory:
    """短期记忆：喂 LLM 的跨轮上下文的权威存储，多行记录按 id 排序。

    role 取值：summary（累积摘要）/ user / assistant。喂 LLM 时由 _expand_record 按 assistant 的
    tool_calls 展开出 tool 空串占位。
    assistant 带 tool_calls 时 content 存 JSON {"tool_calls":[...]}。
    完整历史正文由 ToolResultStore 落盘 _history.txt，供 read_file 读回细节。
    """

    async def get_records(self, user_id, conversation_id):
        """返回该会话短期记忆的 [{role, content}, ...]（按 id 升序，summary 最前）；无则空列表。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select role, content from short_term_memory "
                "where conversation_id=$1 order by id", conversation_id)
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def append_records(self, user_id, conversation_id, records):
        """追加消息到短期记忆（不清空旧记录）；records 为 [{role, content}, ...]。"""
        async with rls_conn(user_id) as conn:
            await conn.executemany(
                "insert into short_term_memory(conversation_id, role, content) values($1, $2, $3)",
                [(conversation_id, rec["role"], rec["content"]) for rec in records])
        logger.info("[记忆:short_term] 追加短期记忆：%s（+%d 条）", conversation_id, len(records))

    async def replace_records(self, user_id, conversation_id, records):
        """全量替换该会话的短期记忆（清空 + 重写），压缩后写回 summary + 剩余消息时用。"""
        async with rls_conn(user_id) as conn:
            await conn.execute("delete from short_term_memory where conversation_id=$1", conversation_id)
            await conn.executemany(
                "insert into short_term_memory(conversation_id, role, content) values($1, $2, $3)",
                [(conversation_id, rec["role"], rec["content"]) for rec in records])
        logger.info("[记忆:short_term] 更新短期记忆：%s（%d 条记录）", conversation_id, len(records))
