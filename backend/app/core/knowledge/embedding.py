# app/core/knowledge/embedding.py
# Embedding 客户端：知识库与长期记忆共用的向量化管道。
# 与 get_chat_model() 的单例工厂同构（均为进程级单例），但**不做降级兜底**：
# 知识库没有向量就检索不到，属于必须失败的情形；长期记忆允许降级，由 store 自己接异常。
import asyncio
import logging
from abc import ABC, abstractmethod

import httpx
from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)


class EmbeddingClient(ABC):
    """向量化接口：所有实现（真实或 Fake）都需实现 embed()"""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化，返回顺序与 texts 一一对应。"""
        ...


class OpenAICompatEmbedding(EmbeddingClient):
    """OpenAI 兼容协议的 Embedding 实现（百炼 DashScope 的 /embeddings 端点）"""

    def __init__(self, base_url, api_key, model, dimensions):
        self.model = model
        self.dimensions = dimensions
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=httpx.Timeout(60.0, connect=10.0))

    async def embed(self, texts):
        # dimensions 显式传入：换模型时靠它锁定维度，避免与建表的 vector(N) 悄悄对不上
        resp = await self.client.embeddings.create(
            model=self.model, input=texts, dimensions=self.dimensions)
        return [item.embedding for item in resp.data]


# 进程级单例：知识库与长期记忆共用同一个 embedder
_embedding: EmbeddingClient | None = None


def get_embedding() -> EmbeddingClient:
    """返回全局 embedder 单例（首次调用时构造）。"""
    global _embedding
    if _embedding is None:
        _embedding = OpenAICompatEmbedding(
            base_url=settings.llm_base_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    return _embedding


# DashScope 的 embedding 接口对单次 input 条数有上限，超上限直接报错；
# 长文档切出几十块时不能一把传，按此分批。
_EMBED_BATCH = 10
# 批次并发数：总耗时约为批数的 _EMBED_CONCURRENCY 分之一，同时不给接口打满并发
_EMBED_CONCURRENCY = 8
# 进度日志的批次间隔，只在批次超过这个数的长文档上打点
_PROGRESS_EVERY = 20


async def embed_batched(embedder: EmbeddingClient, texts: list[str]) -> list[list[float]]:
    """分批并发调用 embed()，返回顺序与 texts 一一对应。

    批次够多时按 _PROGRESS_EVERY 打进度：长文档向量化要跑几十秒到几分钟，
    中途一行不打看起来就像卡死。
    """
    if not texts:
        return []
    batches = [texts[i:i + _EMBED_BATCH] for i in range(0, len(texts), _EMBED_BATCH)]
    sem = asyncio.Semaphore(_EMBED_CONCURRENCY)
    total = len(texts)
    report = len(batches) > _PROGRESS_EVERY
    done = 0
    items = 0

    async def run(batch: list[str]) -> list[list[float]]:
        nonlocal done, items
        async with sem:
            out = await embedder.embed(batch)
        # 两行计数之间没有 await，单线程事件循环下不会交错
        done += 1
        items += len(batch)
        if report and (done % _PROGRESS_EVERY == 0 or done == len(batches)):
            logger.info("[embedding] 向量化进度 %d/%d 批（%d/%d 条）",
                        done, len(batches), items, total)
        return out

    # gather 按入参顺序返回，逐批展开即与 texts 对齐
    return [v for batch in await asyncio.gather(*(run(b) for b in batches)) for v in batch]


def to_vector_literal(vec: list[float]) -> str:
    """转成 pgvector 的文本字面量 '[0.1,0.2,...]'。

    asyncpg 不认识 vector 类型，传字符串再在 SQL 里 cast 是最省事的做法，
    免得为了一个类型引入 pgvector 库的 codec 注册（这套代码只写不读向量列）。
    """
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"
