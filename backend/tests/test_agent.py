# tests/test_agent.py
# Agent ReAct 循环契约测试：最终答案累积、工具调用并行、结构化截断。
# 用 ScriptedLLM 预设 tool_calls 序列、FakeRegistry 模拟工具执行，不依赖真实 API。
# 主循环由 create_agent 驱动，模型入口是 langchain 模型（EventScriptChatModel 把逐轮事件脚本适配进来）。
import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages.utils import convert_to_openai_messages
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

import app.core.agent as agent_mod
from app.config import settings
from app.core.agent import RECURSION_LIMIT, Agent, CriticConfig, _CriticMiddleware


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
    """工具注册表的假实现：call_raw 记录调用并返回预设结果。"""

    def __init__(self, r, raises=None):
        self.r = r
        self.raises = raises or {}  # name → 要抛的异常，驱动工具失败的异常出口
        self.called = []

    async def call_raw(self, name, args):
        self.called.append((name, args))
        if name in self.raises:
            raise self.raises[name]
        return self.r[name]

    def get(self, name):
        # create_agent 需要真工具对象（middleware 才拿得到 request.tool）；执行由 middleware 短路，不会真跑
        return _StubTool(name)

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
        # 默认延迟保持一致：所有工具同时起步，重叠窗口才观测得到
        await asyncio.sleep(self.delays.get(name, 0.05))
        self.active -= 1
        return self.results[name]

    def get(self, name):
        return _StubTool(name)

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
async def test_run_stream_returns_accumulated_answer():
    """验证 run_stream 无工具轮：多个 content 块累积成完整答案并作为返回值交出。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "成"}, {"type": "content", "text": "都晴"}, {"type": "end", "tool_calls": None}],
    ])
    a = Agent(name="t", system_prompt="s", tools=[], model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("查天气", [], FakeRegistry({}))
    assert out == "成都晴"


@pytest.mark.asyncio
async def test_run_stream_tool_call_and_accumulated_text():
    """验证 run_stream 工具调用：思考（调工具轮）不计入回答，返回的只有最终答案。"""
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
    assert reg.called == [("w", {"c": "成都"})]
    assert any(e["type"] == "tool_call" and e["tool"] == "w" and e["args"] == {"c": "成都"} for e in events)
    assert any(e["type"] == "tool_result" and e["tool"] == "w" and e["status"] is True and e["result"] == "晴" for e in events)


@pytest.mark.asyncio
async def test_tool_failure_redacts_secrets(monkeypatch, caplog):
    """工具抛出的异常里带已配置的密钥时，日志与回填文本都只剩 ***。

    异常串会走「日志 / 回填模型 / SSE 下发 / 落库」四条路径，故两处出口都要验。
    """
    secret = "amap-key-should-not-leak"
    monkeypatch.setattr(settings, "amap_api_key", secret)
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "w", "arguments": "{}"}}]}],
        [{"type": "content", "text": "done"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({}, raises={"w": RuntimeError(
        "Client error '400 Bad Request' for url "
        f"'https://restapi.amap.com/v3/geocode/geo?key={secret}&address=%E6%88%90%E9%83%BD'")})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    with caplog.at_level(logging.WARNING, logger="app.core.agent"):
        out, _ = await a.run_stream("go", [], reg, on_event=on_event)

    assert secret not in caplog.text  # 出口 1：stdout 日志
    assert secret not in str(events)  # 出口 2：tool_result SSE（同一份文本也随答案落库）
    assert "***" in caplog.text
    assert out == "done"  # 脱敏不改变「失败回填给模型、本轮照常收口」的行为


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
    # 回填给模型的 tool 消息同样按 tool_call_id 原始顺序，内容与工具一一对应 —— 完成顺序相反也不乱
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b"]
    assert [m["content"] for m in tool_msgs] == ["a_result", "b_result"]


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
    """模型开始产出内容时发一次 status（覆盖生成期的长静默），且先于同轮的 tool_call 事件。"""
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


@pytest.mark.asyncio
async def test_never_converging_model_hits_recursion_limit():
    """模型每轮只回 tool_calls、从不给答案：跑满 60 步后返回兜底文案、rounds 记满。

    额度固定 60 步，一轮 ReAct 占 3 步（before_model 钩子 + 模型 + 工具），故恰好放行 20 轮；
    第 21 次模型调用不会发生。
    """
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": f"c{i}", "function": {"name": "w", "arguments": "{}"}}]}]
        for i in range(20)])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, rounds = await a.run_stream("go", [], reg, on_event=on_event)
    assert out == _FALLBACK
    assert len(rounds) == 20  # 跑满 20 轮，每轮都真实执行了工具
    assert reg.called == [("w", {})] * 20
    assert llm.i == 20


def test_recursion_limit_is_pinned_at_60():
    """图侧额度常量就是写死的 60：一轮 ReAct 占 3 步（before_model 钩子 + 模型 + 工具），即最多 20 轮。"""
    assert RECURSION_LIMIT == 60


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
              model=EventScriptChatModel(llm=llm))
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
              model=EventScriptChatModel(llm=llm))
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


@pytest.mark.asyncio
async def test_critic_loop_stops_at_max_rewrite(monkeypatch):
    """连续判不通过：critic 调 max_rewrite + 1 次后退出、correcting 发 max_rewrite 次、不挂死。"""
    llm = StreamingLLM([_tool_round("c1"), _answer_round("初版，机票100元"),
                        _answer_round("改写一"), _answer_round("改写二")])
    calls = _install_critic(monkeypatch, [
        {"ok": False, "issues": [{"claim": "票价", "problem": "编造", "correction": "删除"}]}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm))
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
              model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    _, rounds = await a.run_stream("成都天气", [], reg, on_event=on_event, critic=CriticConfig())
    assert len(calls) == 2  # 校验与复判确实发生了
    assert rounds == [[{"id": "c1", "type": "function", "function": {"name": _HARD_TOOL, "arguments": "{}"}}]]
    assert len([e for e in events if e.get("status") == "checking"]) == 1
    assert llm.i == 3  # 3 次模型调用里只有 1 次是调工具的轮次


@pytest.mark.asyncio
async def test_critic_turn_still_hits_recursion_limit(monkeypatch):
    """永不收敛 + 带 critic：额度与无 critic 时相同，仍恰跑满 20 轮，返回兜底文案。

    第 21 轮模型决策不会发生（额度在第 21 轮的 before_model 钩子处触顶），故 reg 只被调 20 次、
    rounds 只有 20 条。
    """
    llm = StreamingLLM([_tool_round(f"c{i}") for i in range(20)])
    _install_critic(monkeypatch, [{"ok": True, "issues": []}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm))
    events = []

    async def on_event(e):
        events.append(e)

    out, rounds = await a.run_stream("go", [], reg, on_event=on_event, critic=CriticConfig())
    assert out == _FALLBACK
    assert len(rounds) == 20
    assert reg.called == [(_HARD_TOOL, {})] * 20


@pytest.mark.asyncio
async def test_recursion_limit_used_up_by_tool_rounds_skips_critic(monkeypatch):
    """20 轮工具 + 一次答案轮 + 带 critic：落兜底文案，且 critic 一次都没被调用。

    20 轮工具占掉第 1-60 步，答案轮的模型调用与收口钩子都排在更后面，故整轮零校验、
    连答案轮的模型调用都没发生（llm.i 停在 20）。额度放宽到 62 时这一档就收敛出答案，本用例转红。
    """
    assert RECURSION_LIMIT == 60  # 第 61 步是 before_model 钩子步，只放宽到 61 时行为不可分，故常量单独钉死
    llm = StreamingLLM([_tool_round(f"c{i}") for i in range(20)]
                       + [_answer_round("成都明天晴")])
    calls = _install_critic(monkeypatch, [{"ok": True, "issues": []}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm))
    out, rounds = await a.run_stream("go", [], reg, critic=CriticConfig())
    assert out == _FALLBACK
    assert calls == []  # 收口钩子没跑到，critic 零调用
    assert len(rounds) == 20
    assert llm.i == 20  # 答案轮的模型调用没发生


@pytest.mark.asyncio
async def test_critic_runs_on_converging_turn_with_critic():
    """收敛 + 带 critic：在额度用尽的最后一轮才收口，收口钩子仍跑得到、critic 确实被触发。

    19 轮工具占 57 步，收口轮（before_model + 模型 + 收口钩子）用掉第 58-60 步、刚好跑满；
    末尾到 END 差一步超限抛异常，但答案已在钩子返回的 state 更新里下发。
    """
    llm = StreamingLLM([_tool_round(f"c{i}") for i in range(19)]
                       + [_answer_round("成都明天晴"), _verdict_turn(ok=True)])
    chat_model = EventScriptChatModel(llm=llm)
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL], model=chat_model)
    out, rounds = await a.run_stream("成都天气", [], reg, critic=CriticConfig())
    assert out == "成都明天晴"
    assert chat_model.verdict_calls >= 1  # 收口钩子确实跑到并触发了 critic
    assert len(rounds) == 19


class _ProbeAfterAgent(AgentMiddleware):
    """探针收口中间件：记录自己的钩子被调用，不改 state。"""

    def __init__(self, log):
        super().__init__()
        self.log = log

    async def aafter_agent(self, state, runtime):
        self.log.append("probe")
        return None


def _patch_create_agent(monkeypatch, append=None, capture=None):
    """包一层 create_agent：按需在 middleware 链末尾追加中间件 / 捕获真实装配出的链。"""
    real = agent_mod.create_agent

    def build(model, tools=None, system_prompt=None, middleware=None, state_schema=None):
        chain = list(middleware or [])
        if capture is not None:
            capture["chain"] = list(chain)
        if append is not None:
            chain.append(append)
        return real(model, tools=tools, system_prompt=system_prompt,
                    middleware=chain, state_schema=state_schema)

    monkeypatch.setattr(agent_mod, "create_agent", build)


@pytest.mark.asyncio
async def test_after_agent_chain_runs_every_middleware_probe(monkeypatch):
    """收口链从 middleware 列表末尾往开头依次全跑：再挂一个收口中间件只会插到 critic 之前。

    框架给每个带 after_agent 的中间件各建一个节点，出口节点取列表里最后一个（它的钩子最先执行），
    随后沿链回到列表开头、最后到 END —— 故追加收口中间件不会让 critic 失效，依赖漂移探针盯住这条行为。
    """
    order = []
    _patch_create_agent(monkeypatch, append=_ProbeAfterAgent(order))

    async def fake_run_critic(model, tool_results, answer,
                              read_file_tool=None, grep_tool=None, max_iter=3):
        order.append("critic")
        return {"ok": True, "issues": []}

    monkeypatch.setattr("app.core.agent.run_critic", fake_run_critic)
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴")])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("成都天气", [], reg, critic=CriticConfig())
    assert out == "成都明天晴"
    assert order == ["probe", "critic"]  # 后挂的收口中间件先跑，critic 仍被调用


@pytest.mark.asyncio
async def test_critic_is_last_after_agent_middleware(monkeypatch):
    """护栏：真实装配出的 middleware 链里，最后一个带 after_agent 的中间件就是 critic。

    收口链是「列表末尾 → 列表开头」的顺序，critic 必须保持在末尾才能最先执行；
    在它之后再追加一个带收口钩子的中间件时本用例转红（链里只有它一个收口中间件时，
    它在列表里的位置不影响链，挪到列表开头仍绿）。
    """
    captured = {}
    _patch_create_agent(monkeypatch, capture=captured)
    llm = StreamingLLM([_tool_round("c1"), _answer_round("成都明天晴")])
    _install_critic(monkeypatch, [{"ok": True, "issues": []}])
    reg = FakeRegistry({_HARD_TOOL: "晴"})
    a = Agent(name="t", system_prompt="s", tools=[_HARD_TOOL],
              model=EventScriptChatModel(llm=llm))
    await a.run_stream("成都天气", [], reg, critic=CriticConfig())
    # 判据与框架一致：after_agent 或 aafter_agent 任一个被实现，就算一个收口中间件
    after_agent = [m for m in captured["chain"]
                   if type(m).after_agent is not AgentMiddleware.after_agent
                   or type(m).aafter_agent is not AgentMiddleware.aafter_agent]
    assert after_agent, "middleware 链里应有一个带 after_agent 的收口中间件"
    assert isinstance(after_agent[-1], _CriticMiddleware)


# ---------------------------------------------------------------------------
# 误抄检索标记的归一化：模型常把切片正文里的 【图13-2：熊猫塔】 原样抄进答案。
# 那种标记对人没有意义，收敛成引用号 【图13-2】 才有机会被前端渲染成图。
# ---------------------------------------------------------------------------

def test_normalize_image_markers_strips_the_description():
    assert agent_mod.normalize_image_markers("看这里。\n【图13-2：熊猫塔】\n") == "看这里。\n【图13-2】\n"


def test_normalize_image_markers_leaves_a_bare_ref_untouched():
    """模型自己就写对了引用号：不该被二次加工成别的东西。"""
    assert agent_mod.normalize_image_markers("【图13-2】") == "【图13-2】"


def test_normalize_image_markers_rewrites_every_occurrence():
    assert agent_mod.normalize_image_markers("【图13-2：甲】和【图18-1：乙】") == "【图13-2】和【图18-1】"


def test_normalize_image_markers_spans_a_multiline_description():
    """描述被模型折了行也要整条收敛掉，否则正文里会留半截标记。"""
    assert agent_mod.normalize_image_markers("【图13-2：熊猫塔\n夜景】") == "【图13-2】"


def test_normalize_image_markers_leaves_plain_text_alone():
    assert agent_mod.normalize_image_markers("熊猫塔高 69.8 米。") == "熊猫塔高 69.8 米。"


@pytest.mark.asyncio
async def test_run_stream_normalizes_leaked_image_markers():
    """模型把切片里的长标记整条抄进答案时，返回值要是收敛后的形态 ——
    归一化必须发生在答案定稿处，落库与下发才不会各带一份。"""
    llm = StreamingLLM([
        [{"type": "content", "text": "看这个。\n【图13-2：熊猫塔】\n"},
         {"type": "end", "tool_calls": None}],
    ])
    a = Agent(name="t", system_prompt="s", tools=[], model=EventScriptChatModel(llm=llm))
    out, _ = await a.run_stream("熊猫塔", [], FakeRegistry({}))
    assert out == "看这个。\n【图13-2】\n"
