# tests/test_memory.py
# 会话历史 + 长期记忆的契约测试：连 agent_test 跑真实 Postgres，验证两层存储与跨用户隔离
import json

import pytest
from langchain_core.messages import AIMessage

from app.core.memory import ConversationStore, LongTermMemory, ShortTermMemory, ToolResultStore
from app.core.memory import long_term as long_term_module
from app.core.memory.manager import MemoryManager
from app.core.memory.metrics import snapshot


class _BrokenEmbedder:
    """永远失败的 embedder：验证 embedding 服务不可用时长期记忆的降级路径。"""

    async def embed(self, texts):
        raise RuntimeError("embedding 服务不可用")


class _FakeExtractLLM:
    """只实现 ainvoke 的假模型：无论输入什么都返回预设的 facts（供提炼链路测试）。"""

    def __init__(self, facts):
        self._facts = facts

    async def ainvoke(self, messages):
        return AIMessage(content=json.dumps({"facts": self._facts}, ensure_ascii=False))


@pytest.mark.asyncio
async def test_conversation_roundtrip(user_id, make_user):
    """验证会话创建、消息追加、历史读取与跨用户隔离（显式 user_id 条件 + RLS 双保险）。"""
    s = ConversationStore()
    cid = await s.create_conversation(user_id, "成都行")
    await s.add_message(user_id, cid, "user", "成都4天")
    hist = await s.get_history(user_id, cid)
    assert [h["role"] for h in hist] == ["user"]
    assert (await s.list_conversations(user_id))[0]["title"] == "成都行"
    # 隔离：其他用户既看不到会话，也读不到消息
    other = await make_user()
    assert await s.list_conversations(other) == []
    assert await s.get_history(other, cid) == []
    assert await s.belongs_to(other, cid) is False


@pytest.mark.asyncio
async def test_long_term_recall(user_id, make_user, fake_embedder):
    """不带 query 的 recall 按 importance 降序取前 top_n 条，且按 user_id 隔离。"""
    l = LongTermMemory(max_facts=10, embedder=fake_embedder)
    await l.add_facts(user_id, [{"fact": "a", "importance": 0.3},
                                {"fact": "b", "importance": 0.9},
                                {"fact": "c", "importance": 0.5}])
    top = await l.recall(user_id, top_n=2)
    assert [f["fact"] for f in top] == ["b", "c"]
    assert await l.recall(await make_user()) == []  # 跨用户隔离


@pytest.mark.asyncio
async def test_long_term_semantic_recall(user_id, fake_embedder):
    """带 query 的 recall 走语义检索：相关的那条排到前面，压过 importance 排序。"""
    l = LongTermMemory(embedder=fake_embedder)
    await l.add_facts(user_id, [{"fact": "偏好经济型酒店", "importance": 0.3},
                                {"fact": "喜欢自然风光", "importance": 0.9}])
    # 无 query：纯 importance 排序，自然风光在前
    assert [f["fact"] for f in await l.recall(user_id, top_n=2)] == ["喜欢自然风光", "偏好经济型酒店"]
    # 有 query：经济型那条语义更近，排第一
    hits = await l.recall(user_id, query="帮我订个经济型的酒店", top_n=2)
    assert hits[0]["fact"] == "偏好经济型酒店"


@pytest.mark.asyncio
async def test_long_term_semantic_recall_user_isolation(user_id, make_user, fake_embedder):
    """语义检索同样按 user 隔离，检索不到别人的偏好。"""
    l = LongTermMemory(embedder=fake_embedder)
    await l.add_facts(await make_user(), [{"fact": "偏好经济型酒店", "importance": 0.9}])
    assert await l.recall(user_id, query="经济型酒店", top_n=5) == []


@pytest.mark.asyncio
async def test_long_term_degrades_without_embedding(user_id):
    """embedding 不可用时长期记忆仍可用：写入降级为无向量，召回回退 importance 排序。"""
    l = LongTermMemory(embedder=_BrokenEmbedder())
    await l.add_facts(user_id, [{"fact": "喜欢自然风光", "importance": 0.9},
                                {"fact": "偏好经济型酒店", "importance": 0.3}])
    assert [f["fact"] for f in await l.get_all(user_id)] == ["喜欢自然风光", "偏好经济型酒店"]
    # 语义检索会失败，但 recall 吞掉异常后退回 importance，不返回空
    hits = await l.recall(user_id, query="自然风光", top_n=5)
    assert [f["fact"] for f in hits] == ["喜欢自然风光", "偏好经济型酒店"]


@pytest.mark.asyncio
async def test_extract_facts_dedups_semantically(user_id, fake_embedder, tmp_path):
    """提炼时语义去重：措辞不同但同一偏好的 fact 不再新增，改为提升已有条目的 importance。"""
    l = LongTermMemory(embedder=fake_embedder)
    await l.add_facts(user_id, [{"fact": "偏好经济型酒店", "importance": 0.5}])
    manager = MemoryManager(ConversationStore(), ShortTermMemory(), l,
                            ToolResultStore(str(tmp_path)),
                            _FakeExtractLLM([{"fact": "订酒店要经济型", "importance": 0.9}]))

    dedup_before = snapshot().get("dedup_hits", 0)
    await manager.extract_facts(user_id, "帮我订个经济型的酒店")

    facts = await l.get_all(user_id)
    assert len(facts) == 1                          # 没有新增重复条目
    assert facts[0]["importance"] == 0.9            # 改为把已有条目的 importance 提到较大值
    # 埋点增量：提炼出的 fact 命中语义去重，走「只提升不新增」分支一次
    assert snapshot().get("dedup_hits", 0) == dedup_before + 1


class _FakeRlsConn:
    """顶替 rls_conn 的假事务上下文：update_importance 只经它发一条 update。"""

    def __init__(self):
        self.executed = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


@pytest.mark.asyncio
async def test_update_importance_bumps_dedup_metric(monkeypatch):
    """语义去重命中即 dedup_hits +1。

    不依赖真实 Postgres：测试库不可达时其余长期记忆用例整体 skip，这条守卫必须仍然生效。
    """
    conn = _FakeRlsConn()
    monkeypatch.setattr(long_term_module, "rls_conn", lambda user_id: conn)
    l = LongTermMemory()
    before = snapshot().get("dedup_hits", 0)
    await l.update_importance("u1", "偏好经济型酒店", 0.9)
    assert len(conn.executed) == 1                            # 确实走了 update 分支，而非提前返回
    assert snapshot().get("dedup_hits", 0) == before + 1      # 埋点增量恰为一次


@pytest.mark.asyncio
async def test_long_term_prune(user_id, make_user, fake_embedder):
    """验证长期记忆的 importance 淘汰与用户隔离"""
    l = LongTermMemory(max_facts=2, embedder=fake_embedder)
    await l.add_facts(user_id, [{"fact": "a", "importance": 0.9}, {"fact": "b", "importance": 0.3},
                                {"fact": "c", "importance": 0.5}])
    facts = await l.get_all(user_id)
    assert len(facts) == 2 and "b" not in [f["fact"] for f in facts]
    assert await l.get_all(await make_user()) == []  # 跨用户隔离


@pytest.mark.asyncio
async def test_long_term_prune_user_isolation(make_user, fake_embedder):
    """验证容量淘汰按 user_id 隔离：A 超限淘汰时不影响 B 的高分 facts 数量与内容"""
    l = LongTermMemory(max_facts=2, embedder=fake_embedder)
    a, b = await make_user(), await make_user()
    # 用户 B 先存入高分事实，作为隔离基准
    await l.add_facts(b, [{"fact": "B-important", "importance": 0.99}])
    # 用户 A 插入超过 max_facts 的低分 facts，触发淘汰
    await l.add_facts(a, [{"fact": "A-low-1", "importance": 0.1},
                          {"fact": "A-low-2", "importance": 0.2},
                          {"fact": "A-low-3", "importance": 0.3}])
    # A 只保留自己 importance 最高的 max_facts 条
    assert [f["fact"] for f in await l.get_all(a)] == ["A-low-3", "A-low-2"]
    # B 的 facts 数量与内容均不受 A 的淘汰影响
    assert [f["fact"] for f in await l.get_all(b)] == ["B-important"]
