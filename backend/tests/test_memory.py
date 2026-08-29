# tests/test_memory.py
# 记忆 + 会话存储的契约测试：使用 tmp_path 临时 SQLite，验证会话、长期记忆两层及用户隔离
import pytest
from app.core.memory import ConversationStore, LongTermMemory


@pytest.mark.asyncio
async def test_conversation_roundtrip(tmp_path):
    """验证会话创建、消息追加、历史读取与用户隔离"""
    s = ConversationStore(str(tmp_path / "m.db"))
    cid = await s.create_conversation("u1", "成都行")
    await s.add_message(cid, "user", "成都4天")
    hist = await s.get_history("u1", cid)
    assert [h["role"] for h in hist] == ["user"]
    assert s.list_conversations("u1")[0]["title"] == "成都行"
    # 隔离：其他用户看不到
    assert s.list_conversations("u2") == []
    assert await s.get_history("u2", cid) == []


@pytest.mark.asyncio
async def test_long_term_recall(tmp_path):
    """recall 按 importance 降序取前 top_n 条，且按 user_id 隔离。"""
    l = LongTermMemory(str(tmp_path / "m.db"), max_facts=10)
    await l.add_facts("u1", [{"fact": "a", "importance": 0.3},
                             {"fact": "b", "importance": 0.9},
                             {"fact": "c", "importance": 0.5}])
    top = await l.recall("u1", top_n=2)
    assert [f["fact"] for f in top] == ["b", "c"]
    assert await l.recall("u2") == []   # 用户隔离


@pytest.mark.asyncio
async def test_long_term_prune(tmp_path):
    """验证长期记忆的 importance 淘汰与用户隔离"""
    l = LongTermMemory(str(tmp_path / "m.db"), max_facts=2)
    await l.add_facts("u1", [{"fact": "a", "importance": 0.9}, {"fact": "b", "importance": 0.3}, {"fact": "c", "importance": 0.5}])
    facts = await l.get_all("u1")
    assert len(facts) == 2 and "b" not in [f["fact"] for f in facts]
    assert await l.get_all("u2") == []   # 用户隔离


@pytest.mark.asyncio
async def test_long_term_prune_user_isolation(tmp_path):
    """验证容量淘汰按 user_id 隔离：A 超限淘汰时不影响 B 的高分 facts 数量与内容"""
    l = LongTermMemory(str(tmp_path / "m.db"), max_facts=2)
    # 用户 B 先存入高分事实，作为隔离基准
    await l.add_facts("b", [{"fact": "B-important", "importance": 0.99}])
    # 用户 A 插入超过 max_facts 的低分 facts，触发淘汰
    await l.add_facts("a", [{"fact": "A-low-1", "importance": 0.1},
                            {"fact": "A-low-2", "importance": 0.2},
                            {"fact": "A-low-3", "importance": 0.3}])
    # A 只保留自己 importance 最高的 max_facts 条
    a_facts = await l.get_all("a")
    assert [f["fact"] for f in a_facts] == ["A-low-3", "A-low-2"]
    # B 的 facts 数量与内容均不受 A 的淘汰影响
    b_facts = await l.get_all("b")
    assert [f["fact"] for f in b_facts] == ["B-important"]
