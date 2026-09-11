# tests/test_embedding.py
# Embedding 客户端契约测试：用假的 openai client 顶替网络调用，只验请求参数与返回解析。
import pytest

from app.core.embedding import OpenAICompatEmbedding


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
