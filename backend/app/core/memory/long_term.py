# app/core/memory/long_term.py
# 长期记忆存储：long_term_memory 表，跨会话持久偏好，按 user_id 隔离，超容量按 importance 淘汰。
# 每条 fact 写入时向量化，召回收敛为「按当前问题语义检索」；向量链路出问题则降级为 importance 排序。
import logging

from app.core.db import rls_conn
from app.core.embedding import embed_batched, get_embedding, to_vector_literal

logger = logging.getLogger(__name__)


def _affected(status: str) -> int:
    """从 asyncpg 的 DML 状态串（如 'DELETE 3'）里取受影响行数。"""
    return int(status.rsplit(" ", 1)[-1]) if status else 0


class LongTermMemory:
    """长期记忆：存储从对话中提炼出的偏好/事实（long_term_memory），超过容量上限时按 importance 淘汰。"""

    def __init__(self, max_facts=100, embedder=None):
        # 只记容量上限；表结构由 deploy/schema.sql 建，store 不做任何启动期 IO
        self.max_facts = max_facts
        # 进程级单例由 service 在 import 期构造，embedder 延迟到首次使用时才建
        self._embedder = embedder

    @property
    def embedder(self):
        return self._embedder or get_embedding()

    async def add_facts(self, user_id, facts):
        """批量写入多条事实（含向量），并在写入后执行容量淘汰：超限时删除 importance 最低的条目。

        向量化失败不阻断写入：该批 fact 以 embedding=NULL 落库，召回时自动退回 importance 排序。
        长期记忆与知识库不同——它没有向量也仍然可用，所以这里降级而不抛。
        """
        texts = [f["fact"] for f in facts]
        try:
            vectors = await embed_batched(self.embedder, texts)
        except Exception as e:
            logger.warning("[记忆:long_term] 向量化失败，本批以无向量写入：%s", e)
            vectors = [None] * len(texts)

        async with rls_conn(user_id) as conn:
            await conn.executemany(
                "insert into long_term_memory(user_id, fact, importance, embedding) "
                "values($1, $2, $3, $4::vector)",
                [(user_id, f["fact"], f.get("importance", 0.5),
                  to_vector_literal(v) if v is not None else None)
                 for f, v in zip(facts, vectors)])
            # 容量淘汰：仅当「当前用户自己」的 facts 超过 max_facts 时，删除该用户 importance 最低（importance 相同时最早创建）的多余条目。
            # 三重 user_id 过滤实现按用户隔离淘汰，绝不触碰其他用户的数据、不挤占他人配额。
            # GREATEST 而非 max：max 在 Postgres 里是聚合函数，不是 SQLite 那样的两参数标量函数。
            status = await conn.execute(
                "delete from long_term_memory where user_id=$1 and id in ("
                "  select id from long_term_memory where user_id=$1"
                "  order by importance asc, created_at asc"
                "  limit greatest(0, (select count(*) from long_term_memory where user_id=$1) - $2)"
                ")", user_id, self.max_facts)
        inserted, deleted = len(facts), _affected(status)
        if deleted:
            logger.info("[记忆:long_term] 写入 %d 条长期记忆，容量淘汰 %d 条（importance 最低）", inserted, deleted)
        else:
            logger.info("[记忆:long_term] 写入 %d 条长期记忆", inserted)

    async def get_all(self, user_id):
        """返回某用户的全部事实（按 importance 降序，fact 原样不截断），供记忆弹窗展示。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select fact, importance from long_term_memory where user_id=$1 "
                "order by importance desc", user_id)
        return [dict(r) for r in rows]

    async def search(self, user_id, query, top_k=20):
        """按语义检索该用户的长期记忆，返回 [{fact, importance, similarity}]（相似度降序）。

        只覆盖有向量的条目；无向量的历史 fact 不参与检索，由 recall 的 importance 兜底补上。
        """
        vec = (await self.embedder.embed([query]))[0]
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select fact, importance, similarity from match_long_term($1::vector, $2, $3)",
                to_vector_literal(vec), user_id, top_k)
        return [dict(r) for r in rows]

    async def recall(self, user_id, query=None, top_n=20):
        """召回长期记忆：有 query 就语义检索最相关的 top_n 条，否则按 importance 降序取前 top_n 条。

        供组装 long_term_hint 使用，有数量上限，避免提示词被历史偏好撑爆；
        无硬阈值，importance 仅用于 add_facts 内的容量淘汰与降级路径的排序。
        向量链路失败一律降级——长期记忆是锦上添花，不该因为它挂掉整轮对话。
        """
        if query:
            try:
                hits = await self.search(user_id, query, top_k=top_n)
                if hits:
                    logger.info("[记忆:long_term] 语义召回 %d 条（query=%.30s）", len(hits), query)
                    return [{"fact": h["fact"], "importance": h["importance"]} for h in hits]
            except Exception as e:
                logger.warning("[记忆:long_term] 语义召回失败，回退 importance 排序：%s", e)

        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select fact, importance from long_term_memory where user_id=$1 "
                "order by importance desc limit $2", user_id, top_n)
        result = [dict(r) for r in rows]
        logger.info("[记忆:long_term] 召回长期记忆 %d 条（top_n=%d）", len(result), top_n)
        return result

    async def update_importance(self, user_id, fact, importance):
        """更新某条已存在事实的 importance（语义去重时提升其权重而非新增重复条目）。"""
        async with rls_conn(user_id) as conn:
            await conn.execute(
                "update long_term_memory set importance=$1 where user_id=$2 and fact=$3",
                importance, user_id, fact)
        logger.info("[记忆:long_term] 语义去重：提升已有 fact importance=%.2f（%.50s）", importance, fact)
