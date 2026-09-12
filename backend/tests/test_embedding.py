# tests/test_embedding.py
# Embedding 客户端契约测试：用假的 openai client / 假 embedder 顶替网络调用，
# 只验请求参数、返回解析与分批并发行为。
import asyncio

import pytest

from app.core.embedding import (_EMBED_BATCH, _EMBED_CONCURRENCY, OpenAICompatEmbedding,
                                embed_batched)


class _FakeItem:
    def __init__(self, vec):
        self.embedding = vec


class _FakeEmbeddings:
    """记录每次调用的入参，并按 dimensions 造等长假向量返回。"""

    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return type("R", (), {"data": [_FakeItem([0.1] * kw["dimensions"]) for _ in kw["input"]]})()


def _client():
    c = OpenAICompatEmbedding("http://fake", "k", "fake-model", 1024)
    fake = _FakeEmbeddings()
    c.client = type("C", (), {"embeddings": fake})()
    return c, fake


@pytest.mark.asyncio
async def test_embed_returns_one_vector_per_text():
    """返回条数与入参一一对应，维度取自构造参数。"""
    c, _ = _client()
    out = await c.embed(["a", "b", "c"])
    assert len(out) == 3
    assert all(len(v) == 1024 for v in out)


@pytest.mark.asyncio
async def test_embed_passes_model_input_and_dimensions():
    """model / input / dimensions 三个参数都要原样传给接口。"""
    c, fake = _client()
    await c.embed(["杭州西湖"])
    kw = fake.calls[0]
    assert kw["model"] == "fake-model"
    assert kw["input"] == ["杭州西湖"]
    assert kw["dimensions"] == 1024


class _RecordingEmbedder:
    """记录批次与并发峰值，向量取入参本身，便于断言返回顺序。"""

    def __init__(self, delay: float = 0.0):
        self.batches: list[list[str]] = []
        self.peak_inflight = 0
        self._inflight = 0
        self._delay = delay

    async def embed(self, texts):
        self.batches.append(list(texts))
        self._inflight += 1
        self.peak_inflight = max(self.peak_inflight, self._inflight)
        if self._delay:
            await asyncio.sleep(self._delay)
        self._inflight -= 1
        return [[float(t)] for t in texts]


@pytest.mark.asyncio
async def test_embed_batched_splits_into_batches_and_keeps_order():
    """按 _EMBED_BATCH 分批（末批可短），返回顺序与入参一一对应。"""
    emb = _RecordingEmbedder()
    texts = [str(i) for i in range(25)]
    out = await embed_batched(emb, texts)

    assert [len(b) for b in emb.batches] == [_EMBED_BATCH, _EMBED_BATCH, 5]
    assert out == [[float(i)] for i in range(25)]


@pytest.mark.asyncio
async def test_embed_batched_runs_batches_concurrently_within_limit():
    """批次并发跑（并发峰值等于上限），不是一批等一批。"""
    emb = _RecordingEmbedder(delay=0.05)
    await embed_batched(emb, [str(i) for i in range(_EMBED_BATCH * 20)])

    assert emb.peak_inflight == _EMBED_CONCURRENCY


@pytest.mark.asyncio
async def test_embed_batched_empty_input_makes_no_call():
    """空输入不发请求，直接返回空列表。"""
    emb = _RecordingEmbedder()
    assert await embed_batched(emb, []) == []
    assert emb.batches == []
