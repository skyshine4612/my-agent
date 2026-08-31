# tests/test_working.py
# 工作记忆契约测试：token 预算淘汰 + LLM 摘要兜底（成对淘汰不拆散配对、保留固定头）
import pytest

from app.core.memory import WorkingMemory


class FakeLLM:
    """记录 complete 调用并返回固定摘要的 LLM 假实现，供断言蒸馏触发与摘要生成。"""

    def __init__(self, summary="摘要"):
        self.summary = summary
        self.complete_calls = 0

    async def complete(self, messages):
        self.complete_calls += 1
        return self.summary


@pytest.mark.asyncio
async def test_fit_no_eviction_when_under_budget():
    """未超预算时原样返回，不触发 LLM 蒸馏。"""
    llm = FakeLLM()
    wm = WorkingMemory(llm, budget_tokens=10000)
    window = [{"role": "user", "content": "hi"}]
    out = await wm.fit(window)
    assert out == window
    assert llm.complete_calls == 0


@pytest.mark.asyncio
async def test_fit_evicts_and_distills():
    """超预算时成对淘汰最老交互单元，被淘汰单元 LLM 蒸馏成 [早期交互摘要] 放回最前。"""
    llm = FakeLLM("已查泰安天气晴33℃")
    wm = WorkingMemory(llm, budget_tokens=60)  # 小预算强制淘汰
    window = [
        # 最老单元（将被淘汰）
        {"role": "assistant", "content": "查天气",
         "tool_calls": [{"id": "1", "function": {"name": "weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 100},
        # 最新单元（保留）
        {"role": "assistant", "content": "查POI",
         "tool_calls": [{"id": "2", "function": {"name": "poi", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "y" * 10},
    ]
    out = await wm.fit(window)
    # 摘要作为 system 消息放回最前
    assert out[0]["role"] == "system"
    assert "[早期交互摘要]" in out[0]["content"]
    assert llm.complete_calls == 1
    # 最新单元的 assistant+tool 配对保留，未被拆散
    tool_msgs = [m for m in out if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["2"]


@pytest.mark.asyncio
async def test_fit_evicts_pairwise_not_split():
    """淘汰以「assistant+tool 单元」为粒度，不会只淘汰 assistant 留下孤悬的 tool 消息。"""
    llm = FakeLLM()
    wm = WorkingMemory(llm, budget_tokens=50)
    window = [
        {"role": "assistant", "content": "查A",
         "tool_calls": [{"id": "1", "function": {"name": "a", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 80},
        {"role": "assistant", "content": "查B",
         "tool_calls": [{"id": "2", "function": {"name": "b", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "z"},
    ]
    out = await wm.fit(window)
    # 剩余消息中，每个 tool 消息之前必有一个对应 assistant(tool_calls)，配对完整
    for i, m in enumerate(out):
        if m.get("role") == "tool":
            # 前一条是 assistant（带 tool_calls），配对未被拆散
            prev = out[i - 1]
            assert prev.get("role") == "assistant" and prev.get("tool_calls")
