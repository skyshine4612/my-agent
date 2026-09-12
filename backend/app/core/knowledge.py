# app/core/knowledge.py
# 知识库存储：documents 表 + 切块 + 向量化入库 + 语义检索。
# 全局共享（不像长期记忆那样按 user 隔离），但读写仍走 rls_conn：
# 1A 的规矩是放进这套栈的表全开 RLS，业务查询一律不裸池直连。
import logging
import time

from app.core.chunking import chunk_text
from app.core.db import rls_conn
from app.core.embedding import embed_batched, get_embedding, to_vector_literal

logger = logging.getLogger(__name__)


class KnowledgeStore:
    """知识库：全局共享的攻略语料，按余弦相似度检索。"""

    def __init__(self, embedder=None, chunk_size=600, overlap=100):
        # 零 IO：进程级单例在 import 时构造 store，连接池要到 lifespan 才建
        self._embedder = embedder
        self.chunk_size = chunk_size
        self.overlap = overlap

    @property
    def embedder(self):
        return self._embedder or get_embedding()

    async def ingest(self, user_id, text, title=None, source_url=None) -> int:
        """切块 → 向量化 → 入库，返回写入的块数。

        embed 失败直接抛出，不静默降级：知识库没有向量就检索不到，
        属于必须让调用方知道的情形（与长期记忆允许 embedding=NULL 相反）。
        """
        started = time.monotonic()
        chunks = chunk_text(text, self.chunk_size, self.overlap)
        if not chunks:
            return 0
        name = title or source_url or "未命名"
        logger.info("[知识库] 切块 %d 块，开始向量化：%s", len(chunks), name)
        vectors = await embed_batched(self.embedder, chunks)
        async with rls_conn(user_id) as conn:
            await conn.executemany(
                "insert into documents(title, content, chunk_index, embedding, source_url) "
                "values($1, $2, $3, $4::vector, $5)",
                [(title, c, i, to_vector_literal(v), source_url)
                 for i, (c, v) in enumerate(zip(chunks, vectors))])
        logger.info("[知识库] 入库：%s（%d 块，耗时 %.1fs）", name, len(chunks),
                    time.monotonic() - started)
        return len(chunks)

    async def search(self, user_id, query, top_k=3) -> list[dict]:
        """语义检索 top-k，返回 [{id, title, content, source_url, similarity}]（相似度降序）。"""
        vec = (await self.embedder.embed([query]))[0]
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select id, title, content, source_url, similarity "
                "from match_documents($1::vector, $2)",
                to_vector_literal(vec), top_k)
        return [dict(r) for r in rows]

    async def list_documents(self, user_id) -> list[dict]:
        """按来源聚合成文档列表（供前端知识库弹窗展示），不含向量列。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select coalesce(title, source_url, '未命名') as title, "
                "       source_url, count(*) as chunks, max(created_at) as created_at "
                "from documents group by 1, 2 order by 3 desc")
        return [dict(r) for r in rows]

    async def delete_by_source(self, user_id, source_url) -> int:
        """按来源删掉整篇文档的所有块，返回删除块数。"""
        async with rls_conn(user_id) as conn:
            res = await conn.execute("delete from documents where source_url = $1", source_url)
        return int(res.split()[-1])
