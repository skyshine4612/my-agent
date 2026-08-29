# tests/test_agent.py
# Agent ReAct 循环与 WorkingMemory 摘要压缩的契约测试：
# 用 ScriptedLLM 预设 tool_calls 序列、FakeRegistry 模拟工具执行，不依赖真实 API。
import asyncio

import pytest

from app.core.agent import Agent, WorkingMemory


class ScriptedLLM:
    """按脚本顺序返回预设回复的 LLM 假实现：chat 依序吐 turns，complete 固定返回摘要。"""
    def __init__(self, turns):
        self.turns = list(turns)
        self.i = 0

    async def chat(self, messages, tools=None, response_format=None):
        r = self.turns[self.i]
        self.i += 1
        return r

    async def complete(self, messages):
        return "摘要"


class FakeRegistry:
    """工具注册表的假实现：call 记录调用并返回预设结果，to_openai_schemas 返回空 schema。"""
    def __init__(self, r):
        self.r = r
        self.called = []

    async def call(self, name, args):
        self.called.append((name, args))
        return self.r[name]

    def to_openai_schemas(self, names):
        return []


@pytest.mark.asyncio
async def test_working_memory_compression():
    """验证摘要压缩：超长消息列表被蒸馏成少量摘要消息（保留在 system 摘要里），而非简单截断。"""
    llm = ScriptedLLM([])
    wm = WorkingMemory(llm, max_tokens=50)
    messages = [{"role": "user", "content": "x" * 100} for _ in range(30)]
    out = await wm.fit(messages)
    assert len(out) < len(messages)
    assert out[0]["role"] == "system" and "[早期对话摘要]" in out[0]["content"]


class StreamingLLM:
    """按脚本产出流式事件的 LLM 假实现：stream_chat 依序吐 turns，并记录每次收到的 messages。"""
    def __init__(self, turns):
        self.turns = list(turns)  # 每项是一次 stream_chat 调用产出的事件列表
        self.i = 0
        self.calls = []          # 记录每次 stream_chat 收到的 messages，供断言回填顺序

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        turn = self.turns[self.i]
        self.i += 1
        for event in turn:
            yield event

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "", "tool_calls": None}

    async def complete(self, messages):
        return "摘要"


class ConcurrentRegistry:
    """可观测并发度的工具注册表假实现：记录同时处于执行中的工具数，验证并行执行。"""
    def __init__(self, results):
        self.results = results
        self.called = []
        self.active = 0
        self.max_active = 0

    async def call(self, name, args):
        self.called.append(name)
        # 进入时自增并发数，退出前 sleep 制造重叠窗口，若串行执行则 max_active 恒为 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        return self.results[name]

    def to_openai_schemas(self, names):
        return []


@pytest.mark.asyncio
async def test_run_stream_tokens_in_order():
    """验证 run_stream 流式 token：content 增量按序经 on_event 发出，最终返回累积文本。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "成"}, {"type": "content", "text": "都晴"}, {"type": "end", "tool_calls": None}],
    ])
    a = Agent(name="t", system_prompt="s", tools=[], llm=llm)
    events = []
    async def on_event(e):
        events.append(e)
    out = await a.run_stream("查天气", [], FakeRegistry({}), on_event=on_event)
    assert out == "成都晴"
    assert [e["content"] for e in events if e["type"] == "token"] == ["成", "都晴"]


@pytest.mark.asyncio
async def test_run_stream_tool_call_and_accumulated_text():
    """验证 run_stream 工具调用：先思考后调工具，工具执行后给出答案；所有流出文本（含思考）都算回答。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "查"}, {"type": "end", "tool_calls": [{"id": "1", "function": {"name": "w", "arguments": "{\"c\":\"成都\"}"}}]}],
        [{"type": "content", "text": "成都晴"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], llm=llm)
    events = []
    async def on_event(e):
        events.append(e)
    out = await a.run_stream("查天气", [], reg, on_event=on_event)
    # 累积文本 = 第一轮思考 "查" + 第二轮答案 "成都晴"
    assert out == "查成都晴"
    assert reg.called == [("w", {"c": "成都"})]
    assert any(e["type"] == "tool_call" and e["tool"] == "w" and e["args"] == {"c": "成都"} for e in events)
    assert any(e["type"] == "tool_result" and e["tool"] == "w" and e["summary"] == "晴" for e in events)


@pytest.mark.asyncio
async def test_run_stream_parallel_tools_keep_order():
    """验证 run_stream 同一轮多工具并行执行，且结果按 tool_calls 原始顺序回填（tool_call_id 对齐）。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [
            {"id": "1", "function": {"name": "a", "arguments": "{}"}},
            {"id": "2", "function": {"name": "b", "arguments": "{}"}},
        ]}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = ConcurrentRegistry({"a": "a_result", "b": "b_result"})
    a = Agent(name="t", system_prompt="s", tools=["a", "b"], llm=llm)
    out = await a.run_stream("go", [], reg, on_event=None)
    assert out == "done"
    # 并发度达到 2，证明两个工具是并行执行而非串行
    assert reg.max_active == 2
    # 第二轮回填的 tool 消息按 tool_call_id 原始顺序（1 在前、2 在后），内容与工具一一对应
    second_messages = llm.calls[1]
    tool_msgs = [m for m in second_messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["1", "2"]
    assert [m["content"] for m in tool_msgs] == ["a_result", "b_result"]


@pytest.mark.asyncio
async def test_run_stream_truncates_tool_result():
    """验证 run_stream 工具结果按 4000 字符截断并追加 [已截断] 标记。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "big", "arguments": "{}"}}]}],
        [{"type": "content", "text": "ok"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"big": "x" * 5000})
    a = Agent(name="t", system_prompt="s", tools=["big"], llm=llm)
    out = await a.run_stream("go", [], reg, on_event=None)
    assert out == "ok"
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert len(tool_msgs[0]["content"]) == 4000 + len("[已截断]")
    assert tool_msgs[0]["content"].endswith("[已截断]")


class CapturingLLM:
    """记录 complete 收到的消息的 LLM 假实现，用于断言蒸馏前的序列化结果。"""
    def __init__(self):
        self.seen = None

    async def complete(self, messages):
        self.seen = messages
        return "摘要"


@pytest.mark.asyncio
async def test_working_memory_fit_serializes_tool_messages():
    """验证 WorkingMemory.fit 蒸馏前把 assistant(tool_calls)/tool(tool_call_id) 序列化为可读文本。"""
    llm = CapturingLLM()
    wm = WorkingMemory(llm, max_tokens=1)  # 极小预算强制触发压缩
    messages = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "function": {"name": "weather", "arguments": "{\"city\":\"成都\"}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "晴"},
        {"role": "assistant", "content": "成都晴"},
    ]
    out = await wm.fit(messages)
    assert len(out) < len(messages)
    # 喂给 complete 的消息（除 system 提示外）不再含结构化字段，且工具调用/结果被改写为可读文本
    assert llm.seen is not None
    for m in llm.seen[1:]:
        assert "tool_calls" not in m and "tool_call_id" not in m
    assert "[工具调用" in llm.seen[2]["content"] and "weather" in llm.seen[2]["content"]
    assert "[工具结果" in llm.seen[3]["content"] and "晴" in llm.seen[3]["content"]
