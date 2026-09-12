# tests/test_agent.py
# Agent ReAct 循环契约测试：最终答案累积、工具调用并行、结构化截断。
# 用 ScriptedLLM 预设 tool_calls 序列、FakeRegistry 模拟工具执行，不依赖真实 API。
# 主循环由 create_agent 驱动，模型入口是 langchain 模型（EventScriptChatModel 把逐轮事件脚本适配进来）。
import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.core.agent import Agent


def _lc_tool_call(tc):
    """OpenAI 形状的 tool_call → langchain 形状（arguments JSON 串解析成 args dict）。"""
    return {"id": tc["id"], "name": tc["function"]["name"],
            "args": json.loads(tc["function"]["arguments"] or "{}")}


class EventScriptChatModel(BaseChatModel):
    """把事件式假 LLM 的 stream_chat 脚本适配成 langchain 模型。

    content 事件拼成答案文本、end 事件的 tool_calls 转 langchain 形状；传给 stream_chat 的 messages
    转回 OpenAI 形状，故对 llm.calls 的断言可按 OpenAI 形状书写。
    """

    llm: Any = None

    @property
    def _llm_type(self):
        return "event-script"

    def bind_tools(self, tools, **kwargs):
        # 覆盖为原样返回自身：create_agent 在调用期才 bind_tools，裸 BaseChatModel 会抛 NotImplementedError
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 事件脚本是 async generator（stream_chat），只能走异步路径；主循环与 critic 都走 ainvoke/astream
        raise NotImplementedError("EventScriptChatModel 只支持异步调用")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        events = [e async for e in self.llm.stream_chat(convert_to_openai_messages(messages))]
        content = "".join(e.get("text", "") for e in events if e.get("type") == "content")
        calls = next((e.get("tool_calls") for e in events if e.get("type") == "end"), None)
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=content, tool_calls=[_lc_tool_call(tc) for tc in (calls or [])]))])


class StreamingLLM:
    """按脚本产出流式事件的 LLM 假实现：stream_chat 依序吐 turns，并记录每次收到的 messages。"""

    def __init__(self, turns):
        self.turns = list(turns)  # 每项是一次 stream_chat 调用产出的事件列表
        self.i = 0
        self.calls = []  # 记录每次 stream_chat 收到的 messages，供断言回填顺序

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        turn = self.turns[self.i]
        self.i += 1
        for event in turn:
            yield event


class FakeRegistry:
    """工具注册表的假实现：call_raw 记录调用并返回预设结果，to_openai_schemas 返回空 schema。"""

    def __init__(self, r):
        self.r = r
        self.called = []

    async def call_raw(self, name, args):
        self.called.append((name, args))
        return self.r[name]

    def get(self, name):
        # create_agent 需要真工具对象（middleware 才拿得到 request.tool）；执行由 middleware 短路，不会真跑
        return _StubTool(name)

    def to_openai_schemas(self, names):
        return []

    def label(self, name):
        return name


class ConcurrentRegistry:
    """可观测并发度的工具注册表假实现：记录同时处于执行中的工具数，验证并行执行。"""

    def __init__(self, results, delays=None):
        self.results = results
        self.delays = delays or {}
        self.called = []
        self.active = 0
        self.max_active = 0

    async def call_raw(self, name, args):
        self.called.append(name)
        # 进入时自增并发数，退出前 sleep 制造重叠窗口，若串行执行则 max_active 恒为 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        # 默认延迟保持一致，现有用例 test_run_stream_parallel_tools_keep_order 的行为不受影响
        await asyncio.sleep(self.delays.get(name, 0.05))
        self.active -= 1
        return self.results[name]

    def get(self, name):
        return _StubTool(name)

    def to_openai_schemas(self, names):
        return []

    def label(self, name):
        return name


def _StubTool(name):
    """占位工具条目（与 ToolRegistry.get 返回的 Tool 同形）：fn 是同名 langchain tool，
    仅供 create_agent 装配 schema —— 执行被 middleware 短路，不会真跑。"""

    @tool(name)
    def _stub() -> str:
        """占位工具。"""
        return ""

    return SimpleNamespace(fn=_stub)


@pytest.mark.asyncio
async def test_run_stream_tokens_in_order():
    """验证 run_stream 最终答案：content 累积后按小块经 on_event 发出，最终返回完整答案。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "成"}, {"type": "content", "text": "都晴"}, {"type": "end", "tool_calls": None}],
    ])
    a = Agent(name="t", system_prompt="s", tools=[], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("查天气", [], FakeRegistry({}), on_event=on_event)
    assert out == "成都晴"
    # 最终答案按小块上发（合并后等于完整答案）
    assert "".join(e["content"] for e in events if e["type"] == "token") == "成都晴"


@pytest.mark.asyncio
async def test_run_stream_tool_call_and_accumulated_text():
    """验证 run_stream 工具调用：思考（调工具轮）不上发不计入回答，只有最终答案上发并返回。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "查"},
         {"type": "end", "tool_calls": [{"id": "1", "function": {"name": "w", "arguments": "{\"c\":\"成都\"}"}}]}],
        [{"type": "content", "text": "成都晴"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("查天气", [], reg, on_event=on_event)
    # 回答只含最终答案，思考 "查" 不计入
    assert out == "成都晴"
    assert "查" not in "".join(e["content"] for e in events if e["type"] == "token")
    assert reg.called == [("w", {"c": "成都"})]
    assert any(e["type"] == "tool_call" and e["tool"] == "w" and e["args"] == {"c": "成都"} for e in events)
    assert any(e["type"] == "tool_result" and e["tool"] == "w" and e["status"] is True and e["result"] == "晴" for e in events)


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
    a = Agent(name="t", system_prompt="s", tools=["a", "b"], model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("go", [], reg, on_event=None)
    assert out == "done"
    # 并发度达到 2，证明两个工具是并行执行而非串行
    assert reg.max_active == 2
    # 第二轮回填的 tool 消息按 tool_call_id 原始顺序（1 在前、2 在后），内容与工具一一对应
    second_messages = llm.calls[1]
    tool_msgs = [m for m in second_messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["1", "2"]
    assert [m["content"] for m in tool_msgs] == ["a_result", "b_result"]


@pytest.mark.asyncio
async def test_run_stream_struct_truncates_tool_result():
    """验证 run_stream 工具结果按结构化截断（str 4000）回填窗口，并追加 [已截断]。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "big", "arguments": "{}"}}]}],
        [{"type": "content", "text": "ok"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"big": "x" * 6000})
    a = Agent(name="t", system_prompt="s", tools=["big"], model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("go", [], reg, on_event=None)
    assert out == "ok"
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert len(tool_msgs[0]["content"]) == 4000 + len("[已截断]")
    assert tool_msgs[0]["content"].endswith("[已截断]")


@pytest.mark.asyncio
async def test_parallel_tool_calls_keep_order_and_ids():
    """同一轮多个 tool_calls：并行执行，tool_call 与 tool_result 事件各带 id 且能按 id 一一配对。

    两个工具的**完成顺序与调用顺序相反**（b 先完成），配对因此必须靠 id 而非队列顺序 ——
    这正是 CLAUDE.md 里「同名工具并发会错配 summary」要防的那件事。
    """
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [
            {"id": "call_a", "function": {"name": "a", "arguments": "{}"}},
            {"id": "call_b", "function": {"name": "b", "arguments": "{}"}},
        ]}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = ConcurrentRegistry({"a": "a_result", "b": "b_result"}, delays={"a": 0.05, "b": 0.0})
    a = Agent(name="t", system_prompt="s", tools=["a", "b"], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("go", [], reg, on_event=on_event)
    assert out == "done"
    assert reg.max_active == 2  # 两个工具确实并行执行

    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(calls) == len(results) == 2
    # tool_call 按 LLM 给出的顺序发出
    assert [e["id"] for e in calls] == ["call_a", "call_b"]
    # tool_result 按 id 配对，而非按完成顺序 —— b 更快完成，故到达顺序与 tool_call 相反
    assert [e["id"] for e in results] == ["call_b", "call_a"]
    assert {e["id"]: e["result"] for e in results} == {"call_a": "a_result", "call_b": "b_result"}


@pytest.mark.asyncio
async def test_model_sees_user_message_last():
    """模型所见窗口的布局：本轮用户指令在最末尾。

    用户指令压在最后是刻意设计（保持最新、不被淘汰）；state 只追加，故这条顺序由
    _WorkingMemoryMiddleware.awrap_model_call 在「模型所见」这一层复位 —— 它直接决定模型
    实际读到的 prompt，丢了等于静默改了提示词。
    """
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "c1", "function": {"name": "w", "arguments": "{}"}}]}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("go", [], reg)
    assert out == "done"
    # 第二轮模型调用所见：system → assistant(tool_calls) → tool → user，用户指令压在最后
    assert [m["role"] for m in llm.calls[1]] == ["system", "assistant", "tool", "user"]
    assert llm.calls[1][-1]["content"] == "go"


@pytest.mark.asyncio
async def test_status_event_precedes_first_content_block():
    """模型开始产出内容时发一次 status（覆盖生成期的长静默），且先于同轮的 tool_call 事件与 token 事件。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "想"},
         {"type": "end", "tool_calls": [{"id": "c1", "function": {"name": "w", "arguments": "{}"}}]}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("go", [], reg, on_event=on_event)
    assert out == "done"
    # 首个事件即 status：调工具轮的思考 "想" 也要先让前端进入「整理中」，而不是先冒工具气泡
    assert events[0] == {"type": "status", "status": "generating"}
    kinds = [e["type"] for e in events]
    assert kinds.index("status") < kinds.index("tool_call")
    # token 事件（最终答案）之前必须先有 status
    assert kinds.index("status") < kinds.index("token")


@pytest.mark.asyncio
async def test_never_converging_model_hits_max_iters_limit():
    """模型每轮只回 tool_calls、从不给答案：跑满 max_iters 后返回兜底文案，不发 token，rounds 记满。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "c1", "function": {"name": "w", "arguments": "{}"}}]}],
        [{"type": "end", "tool_calls": [{"id": "c2", "function": {"name": "w", "arguments": "{}"}}]}],
        [{"type": "end", "tool_calls": [{"id": "c3", "function": {"name": "w", "arguments": "{}"}}]}],
    ])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    events = []

    async def on_event(e):
        events.append(e)

    out, rounds = await a.run_stream("go", [], reg, on_event=on_event)
    assert out == "抱歉，本轮查询了较多信息但未能整合成完整回答，请换个方式或精简需求后再试。"
    assert len(rounds) == 3  # 跑满 max_iters 轮，每轮都真实执行了工具
    assert reg.called == [("w", {})] * 3
    assert [e for e in events if e["type"] == "token"] == []


@pytest.mark.asyncio
async def test_default_max_iters_is_20_rounds():
    """默认 max_iters = 20，且真能跑满 20 轮 —— 图侧 recursion_limit = 3 * max_iters = 60。

    一轮 ReAct 在限额里占 3 个任务（abefore_model 钩子 + 模型 + 工具），故上限必须是 3 * max_iters：
    少一档就凑不满 max_iters 轮，多一档会把多出来的模型调用放进去。
    """
    assert Agent(name="t", system_prompt="s").max_iters == 20
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": f"c{i}", "function": {"name": "w", "arguments": "{}"}}]}]
        for i in range(20)])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    out, rounds = await a.run_stream("go", [], reg)
    assert len(rounds) == 20
    assert reg.called == [("w", {})] * 20
    assert out == "抱歉，本轮查询了较多信息但未能整合成完整回答，请换个方式或精简需求后再试。"


@pytest.mark.asyncio
async def test_model_sees_distilled_summary_before_kept_window():
    """淘汰轮的布局：蒸馏摘要在保留窗口之前，用户指令仍在最后。

    摘要位置不是纯显示问题：窗口布局变了，_units() 的切分跟着变，之后每轮的淘汰对象与
    working_evicted_units / working_distill_calls 就会跟着变。
    """
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "c1", "function": {"name": "big", "arguments": "{}"}}]}],
        [{"type": "end", "tool_calls": [{"id": "c2", "function": {"name": "small", "arguments": "{}"}}]}],
        # 淘汰单元的蒸馏同样走这一份脚本：它取走的这个 turn 的文本即淘汰摘要本体
        [{"type": "content", "text": "早期摘要"}, {"type": "end", "tool_calls": None}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"big": "x" * 6000, "small": "ok"})
    a = Agent(name="t", system_prompt="s", tools=["big", "small"], model=EventScriptChatModel(llm=llm))
    # 预算只够留下第二轮的交互：第一轮的大结果被淘汰并蒸馏成摘要
    out, _ = await a.run_stream("go", [], reg, context_budget=600)
    assert out == "done"
    last = llm.calls[3]  # 答案轮的模型所见（calls[2] 是淘汰单元的蒸馏调用）
    assert [m["role"] for m in last] == ["system", "system", "assistant", "tool", "user"]
    assert last[1]["content"] == "[早期交互摘要] 早期摘要"  # 摘要在保留窗口之前
    assert last[-1]["content"] == "go"  # 用户指令在最后
