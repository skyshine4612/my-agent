# tests/test_memory.py
# 会话历史 + 长期记忆的契约测试：连 agent_test 跑真实 Postgres，验证两层存储与跨用户隔离
import pytest

from app.core.memory import ConversationStore, LongTermMemory


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
async def test_long_term_recall(user_id, make_user):
    """recall 按 importance 降序取前 top_n 条，且按 user_id 隔离。"""
    l = LongTermMemory(max_facts=10)
    await l.add_facts(user_id, [{"fact": "a", "importance": 0.3},
                                {"fact": "b", "importance": 0.9},
                                {"fact": "c", "importance": 0.5}])
    top = await l.recall(user_id, top_n=2)
    assert [f["fact"] for f in top] == ["b", "c"]
    assert await l.recall(await make_user()) == []  # 跨用户隔离


@pytest.mark.asyncio
async def test_long_term_prune(user_id, make_user):
    """验证长期记忆的 importance 淘汰与用户隔离"""
    l = LongTermMemory(max_facts=2)
    await l.add_facts(user_id, [{"fact": "a", "importance": 0.9}, {"fact": "b", "importance": 0.3},
                                {"fact": "c", "importance": 0.5}])
    facts = await l.get_all(user_id)
    assert len(facts) == 2 and "b" not in [f["fact"] for f in facts]
    assert await l.get_all(await make_user()) == []  # 跨用户隔离


@pytest.mark.asyncio
async def test_long_term_prune_user_isolation(make_user):
    """验证容量淘汰按 user_id 隔离：A 超限淘汰时不影响 B 的高分 facts 数量与内容"""
    l = LongTermMemory(max_facts=2)
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
