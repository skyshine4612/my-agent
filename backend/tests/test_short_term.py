# tests/test_short_term.py
# 短期记忆（summary + 最近几轮对话）契约测试：replace_records / get_records / append_records
# 短期记忆表有 conversation_id 外键，且 RLS 按会话归属判定，因此每个用例都要先建会话。
import pytest
from langchain_core.messages import AIMessage

from app.core.memory import ConversationStore, ShortTermMemory
from app.core.memory.manager import MemoryManager


@pytest.mark.asyncio
async def test_replace_and_get_records(user_id, new_conversation):
    """replace_records 后 get_records 按序取回（summary 在前，对话在后）。"""
    s = ShortTermMemory()
    cid = await new_conversation()
    records = [
        {"role": "summary", "content": "早期对话摘要"},
        {"role": "user", "content": "最近一轮用户"},
        {"role": "assistant", "content": "最近一轮助手"},
    ]
    await s.replace_records(user_id, cid, records)
    got = await s.get_records(user_id, cid)
    assert [r["role"] for r in got] == ["summary", "user", "assistant"]
    assert got[0]["content"] == "早期对话摘要"
    assert got[1]["content"] == "最近一轮用户"


@pytest.mark.asyncio
async def test_replace_records_overwrites(user_id, new_conversation):
    """同会话再次 replace_records 清空旧记录、只保留新记录。"""
    s = ShortTermMemory()
    cid = await new_conversation()
    await s.replace_records(user_id, cid, [{"role": "user", "content": "旧"}])
    await s.replace_records(user_id, cid, [{"role": "summary", "content": "新摘要"}])
    got = await s.get_records(user_id, cid)
    assert len(got) == 1
    assert got[0]["content"] == "新摘要"


@pytest.mark.asyncio
async def test_records_isolation_between_conversations(user_id, new_conversation):
    """同一用户的不同会话，短期记忆互相隔离。"""
    s = ShortTermMemory()
    c1, c2 = await new_conversation(), await new_conversation()
    await s.replace_records(user_id, c1, [{"role": "summary", "content": "A"}])
    await s.replace_records(user_id, c2, [{"role": "summary", "content": "B"}])
    assert (await s.get_records(user_id, c1))[0]["content"] == "A"
    assert (await s.get_records(user_id, c2))[0]["content"] == "B"


@pytest.mark.asyncio
async def test_records_cross_user_isolation(user_id, new_conversation, make_user):
    """跨用户隔离：B 读不到 A 会话的短期记忆。

    这是 1A 的验收要点——策略若被 BYPASSRLS 绕过，这里会读到 A 的数据而失败。
    """
    s = ShortTermMemory()
    cid = await new_conversation()
    await s.replace_records(user_id, cid, [{"role": "summary", "content": "A 的摘要"}])
    assert await s.get_records(await make_user(), cid) == []


@pytest.mark.asyncio
async def test_cascade_delete_on_conversation(user_id, new_conversation):
    """删除会话后其短期记忆随外键级联消失，其他会话不受影响。"""
    s = ShortTermMemory()
    store = ConversationStore()
    c1, c2 = await new_conversation(), await new_conversation()
    await s.replace_records(user_id, c1, [{"role": "summary", "content": "A"}])
    await s.replace_records(user_id, c2, [{"role": "summary", "content": "B"}])
    assert await store.delete_conversation(user_id, c1) is True
    assert await s.get_records(user_id, c1) == []
    assert len(await s.get_records(user_id, c2)) == 1


@pytest.mark.asyncio
async def test_append_records_with_tool(user_id, new_conversation):
    """append_records 追加消息（不清空旧记录），tool 消息带 tool_call_id。"""
    s = ShortTermMemory()
    cid = await new_conversation()
    await s.append_records(user_id, cid, [
        {"role": "user", "content": "问", "tool_call_id": None},
        {"role": "assistant", "content": '{"tool_calls":[]}', "tool_call_id": None},
        {"role": "tool", "content": "结果", "tool_call_id": "c1"},
        {"role": "assistant", "content": "答", "tool_call_id": None},
    ])
    got = await s.get_records(user_id, cid)
    assert [r["role"] for r in got] == ["user", "assistant", "tool", "assistant"]
    assert got[2]["tool_call_id"] == "c1"
    assert got[2]["content"] == "结果"


class _CapturingLLM:
    """记录提示词的假模型：护栏测试要看的是提示词本身，不是摘要产物。"""

    def __init__(self):
        self.messages = []

    async def ainvoke(self, messages):
        self.messages = messages
        return AIMessage(content="新摘要")


@pytest.mark.asyncio
async def test_summary_prompt_demands_file_names_verbatim():
    """短期摘要的提示词必须点名「文件名原样保留」。

    摘要跨轮持久，是压缩后模型能看见的全部上下文；文件名一旦被概括成「某个 Excel 文件」，
    用户下一轮说「把刚才那个文件的姓名也打码」就没有可指向的对象了。store 传 None 是因为
    这个用例只走 _update_summary 的纯 LLM 分支，不碰任何存储。
    """
    llm = _CapturingLLM()
    m = MemoryManager(None, None, None, None, llm)
    await m._update_summary("旧摘要", "【用户】把测试数据.xlsx 脱敏")
    prompt = llm.messages[0]["content"]
    assert "文件名" in prompt and "原样保留" in prompt
