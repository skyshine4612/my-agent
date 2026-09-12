# tests/test_working.py
# 工作记忆契约测试：token 预算淘汰 + LLM 摘要兜底（成对淘汰不拆散配对、保留固定头）
import pytest
from langchain_core.messages import AIMessage

from app.core.memory import WorkingMemory
from app.core.memory.metrics import snapshot


class FakeLLM:
    """记录蒸馏调用次数并返回固定摘要的假模型，供断言蒸馏触发与摘要生成。"""

    def __init__(self, summary="摘要"):
        self.summary = summary
        self.calls = 0
        self.messages = []

    async def ainvoke(self, messages):
        self.calls += 1
        self.messages = messages
        return AIMessage(content=self.summary)


@pytest.mark.asyncio
async def test_fit_no_eviction_when_under_budget():
    """未超预算时原样返回，不触发 LLM 蒸馏。"""
    llm = FakeLLM()
    wm = WorkingMemory(llm, budget_tokens=10000)
    window = [{"role": "user", "content": "hi"}]
    out = await wm.fit(window)
    assert out == window
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_fit_evicts_and_distills():
    """超预算时成对淘汰最老交互单元，被淘汰单元 LLM 蒸馏成 [早期交互摘要] 放回最前。"""
    llm = FakeLLM("已查泰安天气晴33℃")
    wm = WorkingMemory(llm, budget_tokens=100)  # 预算需大于「最新单元」的 token 数，保证只淘汰最老单元
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
    evicted_before = snapshot().get("working_evicted_units", 0)
    distill_before = snapshot().get("working_distill_calls", 0)
    out = await wm.fit(window)
    # 摘要作为 system 消息放回最前
    assert out[0]["role"] == "system"
    assert "[早期交互摘要]" in out[0]["content"]
    assert llm.calls == 1
    # 埋点增量：淘汰计数按消息条数算（最老单元 = assistant + tool = 2 条），非单元个数；每淘汰一批即一次蒸馏
    assert snapshot().get("working_evicted_units", 0) == evicted_before + 2
    assert snapshot().get("working_distill_calls", 0) == distill_before + 1
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


class FailingLLM:
    """ainvoke 抛异常的假模型，验证蒸馏失败时降级为拼接截断而非抛错。"""

    async def ainvoke(self, messages):
        raise RuntimeError("LLM 超时")


@pytest.mark.asyncio
async def test_fit_distill_failure_falls_back():
    """LLM 蒸馏失败时 fit 不抛异常，降级为序列化文本拼接摘要（保留被淘汰单元内容）。"""
    llm = FailingLLM()
    wm = WorkingMemory(llm, budget_tokens=60)
    window = [
        {"role": "assistant", "content": "查天气",
         "tool_calls": [{"id": "1", "function": {"name": "weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 100},
        {"role": "assistant", "content": "查POI",
         "tool_calls": [{"id": "2", "function": {"name": "poi", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "y" * 10},
    ]
    fallback_before = snapshot().get("working_distill_fallbacks", 0)
    out = await wm.fit(window)  # 不应抛异常
    # 降级摘要作为 system 消息放回最前，且保留被淘汰单元的可读内容（x*100 拼进摘要）
    assert out[0]["role"] == "system"
    assert "[早期交互摘要]" in out[0]["content"]
    assert "x" in out[0]["content"]
    # 埋点增量：蒸馏抛异常走降级分支，恰好记一次
    assert snapshot().get("working_distill_fallbacks", 0) == fallback_before + 1


@pytest.mark.asyncio
async def test_fit_distills_single_oversized_unit():
    """单个单元超预算时也被蒸馏成摘要，而不是原样超预算返回。"""
    llm = FakeLLM("巨单摘要")
    wm = WorkingMemory(llm, budget_tokens=50)
    window = [
        {"role": "assistant", "content": "查天气",
         "tool_calls": [{"id": "1", "function": {"name": "weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 500},  # 单条极大，超预算
    ]
    out = await wm.fit(window)
    # 整个单元被蒸馏成摘要，窗口只剩 1 条 system 摘要
    assert len(out) == 1
    assert out[0]["role"] == "system"
    assert "[早期交互摘要]" in out[0]["content"]


@pytest.mark.asyncio
async def test_distill_prompt_demands_file_names_verbatim():
    """蒸馏提示词必须点名「文件名原样保留」。

    文件名是重跑脚本恢复产物的唯一指针，而「测试数据.xlsx」这类长资源名正是最容易被
    概括成「某个 Excel 文件」的东西——丢了模型就不知道拿哪个文件重跑。这条护栏防的是
    这句要求被当作冗余措辞删掉。
    """
    llm = FakeLLM("摘要")
    wm = WorkingMemory(llm, budget_tokens=50)
    await wm.fit([
        {"role": "assistant", "content": "脱敏",
         "tool_calls": [{"id": "1", "function": {"name": "run_skill_script", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "x" * 500},
    ])
    prompt = llm.messages[0]["content"]
    assert "文件名" in prompt and "原样保留" in prompt
