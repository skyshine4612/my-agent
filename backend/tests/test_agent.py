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

from app.core.agent import Agent, CriticConfig


def _lc_tool_call(tc):
    """OpenAI 形状的 tool_call → langchain 形状（arguments JSON 串解析成 args dict）。"""
    return {"id": tc["id"], "name": tc["function"]["name"],
            "args": json.loads(tc["function"]["arguments"] or "{}")}


def _verdict_turn(ok, issues=None):
    """critic 下结论轮的事件脚本（结构化输出工具名就是 CriticVerdict 模型名）。"""
    return [{"type": "end", "tool_calls": [
        {"id": "v1", "function": {"name": "CriticVerdict",
                                  "arguments": json.dumps({"ok": ok, "issues": issues or []})}}]}]


def _install_critic(monkeypatch, verdicts):
    """用预设判定替换 app.core.agent.run_critic，并记录每次收到的入参（tool_results / answer）。

    脚本用完后一直返回最后一条：够 test_critic_loop_stops_at_max_rewrite 这类「连续判不通过」的用例用。
    """
    calls = []

    async def fake_run_critic(model, tool_results, answer, read_file_tool=None, grep_tool=None, max_iter=3):
        calls.append({"tool_results": tool_results, "answer": answer})
        return verdicts[min(len(calls) - 1, len(verdicts) - 1)]

    monkeypatch.setattr("app.core.agent.run_critic", fake_run_critic)
    return calls


class EventScriptChatModel(BaseChatModel):
    """把事件式假 LLM 的 stream_chat 脚本适配成 langchain 模型。

    content 事件拼成答案文本、end 事件的 tool_calls 转 langchain 形状；传给 stream_chat 的 messages
    转回 OpenAI 形状，故对 llm.calls 的断言可按 OpenAI 形状书写。
    """

    llm: Any = None
    verdict_calls: int = 0  # 返回 CriticVerdict 结论的次数（主循环与 critic 共用这一个模型实例）

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
        msg = AIMessage(content=content, tool_calls=[_lc_tool_call(tc) for tc in (calls or [])])
        if any(tc["name"] == "CriticVerdict" for tc in msg.tool_calls):
            self.verdict_calls += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


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


# ---------------------------------------------------------------------------
# critic 回路：挂在 ReAct 收口钩子（after_agent）上，环跑在钩子内部，对图不可见。
# 触发条件是本轮调用了硬数据工具（weather_query），故用例都以它为工具。
# ---------------------------------------------------------------------------

_HARD_TOOL = "weather_query"
_FALLBACK = "抱歉，本轮查询了较多信息但未能整合成完整回答，请换个方式或精简需求后再试。"


def _tool_round(call_id):
    """一轮「只回 tool_calls」的 ReAct 事件脚本。"""
    return [{"type": "end", "tool_calls": [
        {"id": call_id, "function": {"name": _HARD_TOOL, "arguments": "{}"}}]}]


def _answer_round(text):
    """一轮给出最终答案的事件脚本。"""
    return [{"type": "content", "text": text}, {"type": "end", "tool_calls": None}]


@pytest.mark.asyncio
async def test_critic_receives_react_answer(monkeypatch):
    """critic 收到的待校验答案是 React 轮的产出原文 —— 事实校验回路真的在工作的唯一凭据。

    取错来源（图 state 里那个只有改写才会写的 answer 键）会让首判恒对着空串校验，
    真机上通常直接判通过，整个回路静默失效。
    """
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴")])
    calls = _install_critic(monkeypatch, [{"ok": True, "issues": []}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    out, _ = await a.run_stream("成都天气", [], reg, critic=CriticConfig())
    assert out == "成都明天晴"
    assert len(calls) == 1
    assert calls[0]["answer"] == "成都明天晴"  # 校验的是 React 轮答案原文，不是空串
    assert calls[0]["tool_results"] == json.dumps(["晴"], ensure_ascii=False)  # 校验依据是本轮硬数据语料


@pytest.mark.asyncio
async def test_critic_loop_converges(monkeypatch):
    """首判不通过、复判通过：critic 调 2 次、correcting 只发 1 次、下发与下发之外的都是改写版。"""
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴，机票100元"),
                        _answer_round("成都明天晴")])
    calls = _install_critic(monkeypatch, [
        {"ok": False, "issues": [{"claim": "机票100元", "problem": "编造票价", "correction": "删除票价"}]},
        {"ok": True, "issues": []},
    ])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("成都天气", [], reg, on_event=on_event, critic=CriticConfig())
    assert len(calls) == 2  # 首判 + 改写后的复判
    assert calls[0]["answer"] == "成都明天晴，机票100元"
    assert calls[1]["answer"] == "成都明天晴"  # 复判校验的是改写后的答案
    assert [e for e in events if e.get("status") == "correcting"] == [{"type": "status", "status": "correcting"}]
    assert reg.called == [(_HARD_TOOL, {})]  # 改写不重新调工具
    assert out == "成都明天晴"  # 下发的是改写版
    assert "".join(e["content"] for e in events if e["type"] == "token") == "成都明天晴"


@pytest.mark.asyncio
async def test_critic_loop_stops_at_max_rewrite(monkeypatch):
    """连续判不通过：critic 调 max_rewrite + 1 次后退出、correcting 发 max_rewrite 次、不挂死。"""
    llm = StreamingLLM([_tool_round("c1"), _answer_round("初版，机票100元"),
                        _answer_round("改写一"), _answer_round("改写二")])
    calls = _install_critic(monkeypatch, [
        {"ok": False, "issues": [{"claim": "票价", "problem": "编造", "correction": "删除"}]}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    events = []

    async def on_event(e):
        events.append(e)

    out, _ = await a.run_stream("成都天气", [], reg, on_event=on_event, critic=CriticConfig())
    assert len(calls) == 3  # 首次判定 + max_rewrite 次复判（默认 max_rewrite=2）
    assert len([e for e in events if e.get("status") == "correcting"]) == 2
    assert out == "改写二"  # 上限内最后一次改写的结果照常下发，不挂死也不空手而归
    assert llm.i == 4  # 1 轮工具 + 答案轮 + 2 次改写，改写没有多调一次


@pytest.mark.asyncio
async def test_critic_does_not_leak_rounds(monkeypatch):
    """critic 环内的模型调用不产生 ReAct 轮次：rounds 只数真正调工具的轮，checking 只发一次。"""
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴，机票100元"),
                        _answer_round("成都明天晴")])
    calls = _install_critic(monkeypatch, [
        {"ok": False, "issues": [{"claim": "机票100元", "problem": "编造票价", "correction": "删除票价"}]},
        {"ok": True, "issues": []},
    ])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    events = []

    async def on_event(e):
        events.append(e)

    _, rounds = await a.run_stream("成都天气", [], reg, on_event=on_event, critic=CriticConfig())
    assert len(calls) == 2  # 校验与复判确实发生了
    assert rounds == [[{"id": "c1", "type": "function", "function": {"name": _HARD_TOOL, "arguments": "{}"}}]]
    assert len([e for e in events if e.get("status") == "checking"]) == 1
    assert llm.i == 3  # 3 次模型调用里只有 1 次是调工具的轮次


@pytest.mark.asyncio
async def test_critic_turn_still_hits_max_iters_limit(monkeypatch):
    """永不收敛 + 带 critic：额度抬高 2 步后仍恰跑满 max_iters 轮，返回兜底文案、零 token。

    抬高 2 步的代价在这里可见：第 4 轮模型决策被放行（脚本要备 4 个 turn），但它的工具节点触顶，
    故 reg 只被调 3 次、rounds 只有 3 条 —— 多出来的只是「次收口前的最后一次决策」。
    """
    llm = StreamingLLM([_tool_round(f"c{i}") for i in range(4)])
    _install_critic(monkeypatch, [{"ok": True, "issues": []}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm), max_iters=3)
    events = []

    async def on_event(e):
        events.append(e)

    out, rounds = await a.run_stream("go", [], reg, on_event=on_event, critic=CriticConfig())
    assert out == _FALLBACK
    assert len(rounds) == 3  # 额度每 +3 多放行一轮，+2 不会多放行第 4 轮
    assert reg.called == [(_HARD_TOOL, {})] * 3
    assert [e for e in events if e["type"] == "token"] == []


@pytest.mark.asyncio
async def test_critic_runs_on_converging_turn_with_critic():
    """收敛 + 带 critic：收口钩子在额度内跑到，critic 确实被触发。

    N 轮工具后收口实测需 3N + 3 步额度（N=1 时下限为 6），正好卡在额度边界上：
    max_iters=2 的额度是 3 * 2 + 2 = 8，而收口这轮只用到 6 步。
    """
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴"), _verdict_turn(ok=True)])
    chat_model = EventScriptChatModel(llm=llm)
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL], model=chat_model, max_iters=2)
    out, rounds = await a.run_stream("成都天气", [], reg, critic=CriticConfig())
    assert out == "成都明天晴"
    assert chat_model.verdict_calls >= 1  # 收口钩子确实跑到并触发了 critic
    assert len(rounds) == 1
