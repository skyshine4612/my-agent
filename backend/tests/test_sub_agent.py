# tests/test_sub_agent.py
# call_sub_agent 工具的契约测试：验证委派子任务返回纯文本答案、子任务自己的 critic 会跑、
# 子 Agent 的工具调用沿同一条通道转发、以及触发话术不会静默丢失。
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.core.agent import CriticConfig
from app.core.context import current_emit, current_tool_call_id, current_user_id
from app.core.memory import ToolResultStore
from app.core.registry import ToolRegistry
from app.tools.sub_agent import make_call_sub_agent


def _collector(events):
    """造一个记录型事件回调（run_stream 的 on_event 是 async 的）。"""
    async def emit(event):
        events.append(event)
    return emit


class SubChatModel(BaseChatModel):
    """子 Agent 的假 langchain 模型（子 Agent 的循环由 create_agent 驱动）：直接回答案、不调工具。"""

    @property
    def _llm_type(self):
        return "sub-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="子任务完成"))])


class WeatherThenAnswerModel(BaseChatModel):
    """先调一次硬数据工具（weather_query），再用带换行的答案收口。

    换行是刻意的：返回值若经 str(dict) 转发，换行会被转义成字面量 \\n 挤成一行，
    父 Agent 看到的就是一整行转义文本。下面的断言钉住「回的是纯文本」。
    """

    step: int = 0

    @property
    def _llm_type(self):
        return "sub-weather"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.step += 1
        if self.step == 1:
            msg = AIMessage(content="", tool_calls=[
                {"name": "weather_query", "args": {"city": "杭州"}, "id": "c1"}])
        else:
            msg = AIMessage(content="杭州明天晴\n气温 18/27℃")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class TwoStepChatModel(BaseChatModel):
    """两步假模型：第一步回一次工具调用（让子 Agent 真的落一次 spill），之后回答案。"""

    step: int = 0

    @property
    def _llm_type(self):
        return "sub-two-step"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.step += 1
        if self.step == 1:
            msg = AIMessage(content="", tool_calls=[
                {"name": "list_items", "args": {"city": "北京"}, "id": "c1"}])
        else:
            msg = AIMessage(content="子任务完成")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.mark.asyncio
async def test_call_sub_agent_returns_answer(tmp_path):
    registry = ToolRegistry()

    @tool
    async def get_skill(name: str) -> str:
        """读取业务规则。"""
        return f"{name} 规则"

    registry.register_tool(get_skill, "读取业务规则")

    fn = make_call_sub_agent(SubChatModel(), registry, ["travel"], 32000)
    result = await fn.ainvoke({"title": "查火车票", "task": "查北京到上海的火车票"})
    assert result == "子任务完成"


@pytest.mark.asyncio
async def test_sub_agent_runs_own_critic_on_hard_data(tmp_path):
    """子 Agent 自己调硬数据工具时，它自己的 critic 必须触发。

    这是「委派不绕过事实校验」的护栏：主 Agent 这轮调的是 call_sub_agent，
    它的 hard_results 是空的、critic 不会跑，校验只能落在子 Agent 这一轮。
    status=checking 只由 _CriticMiddleware 发出，出现即证明 critic 跑过。
    """
    registry = ToolRegistry()

    @tool
    async def weather_query(city: str) -> str:
        """查天气。"""
        return "杭州 晴 18/27℃"

    registry.register_tool(weather_query, "查天气")
    events = []
    token = current_emit.set(_collector(events))
    try:
        fn = make_call_sub_agent(WeatherThenAnswerModel(), registry, ["travel"], 32000,
                                 critic=CriticConfig())
        result = await fn.ainvoke({"title": "查杭州天气", "task": "查杭州明天天气"})
    finally:
        current_emit.reset(token)

    assert result == "杭州明天晴\n气温 18/27℃"
    assert "\n" in result, "答案必须保留真实换行，不能被转义成字面量 \\n"
    assert any(e.get("type") == "status" and e.get("status") == "checking" for e in events), \
        "子 Agent 的 critic 没跑"


@pytest.mark.asyncio
async def test_sub_agent_tool_calls_are_forwarded_with_agent_and_parent(tmp_path):
    """子 Agent 的工具调用经 current_emit 转发出去，且带 agent + parent 供前端分层挂载。

    parent 是发起本次委派的那次 call_sub_agent 的 tool_call_id——并行委派时靠它区分
    「哪个子任务在干什么」，前端据此把子任务的进度挂到对应的委派节点下。
    """
    registry = ToolRegistry()

    @tool
    async def list_items(city: str) -> str:
        """查列表。"""
        return "ok"

    registry.register_tool(list_items, "查列表")
    events = []
    token = current_emit.set(_collector(events))
    id_token = current_tool_call_id.set("call_delegation_1")
    try:
        fn = make_call_sub_agent(TwoStepChatModel(), registry, ["travel"], 32000)
        await fn.ainvoke({"title": "查北京", "task": "查北京有什么"})
    finally:
        current_emit.reset(token)
        current_tool_call_id.reset(id_token)

    tool_calls = [e for e in events if e.get("type") == "tool_call"]
    assert [e["tool"] for e in tool_calls] == ["list_items"]
    assert all(e["agent"] == "sub" for e in tool_calls), "子 Agent 事件必须带 agent=sub"
    assert all(e["parent"] == "call_delegation_1" for e in tool_calls), "子 Agent 事件必须带发起它的委派 id"
    # 阶段状态同样带 parent：否则并行子任务的状态会写到全局，前端分不清是谁的
    assert all(e.get("parent") == "call_delegation_1"
               for e in events if e.get("type") == "status")


@pytest.mark.asyncio
async def test_call_sub_agent_cleans_up_sub_conversation(tmp_path):
    """传入 tool_result_store 时，子任务结束要清掉子会话目录（子 Agent 落过的 spill 一并带走）。

    传 store 才是生产形态（service 装配时恒传），不传时 finally 里那段清理不会执行。
    """
    registry = ToolRegistry()

    @tool
    async def list_items(city: str) -> list:
        """查列表。"""
        return [f"{city}-{i}" for i in range(30)]   # 超 list_limit=20，触发地址索引落盘

    registry.register_tool(list_items, "查列表")

    store = ToolResultStore(str(tmp_path))
    token = current_user_id.set("u1")
    try:
        fn = make_call_sub_agent(TwoStepChatModel(), registry, ["travel"], 32000, store)
        result = await fn.ainvoke({"title": "查北京", "task": "查北京有什么"})
    finally:
        current_user_id.reset(token)

    assert result == "子任务完成"
    # 子会话目录连同 spill 文件已清掉；用户目录随之空了也一并删
    assert not (tmp_path / "u1").exists()


def test_description_keeps_trigger_contract():
    """工具描述是模型唯一的触发依据，关键话术不得静默丢失。

    照 hermes 的做法（tests/tools/test_delegate.py 把 execute_code / SELF-REPORTS
    这类词钉死在描述里）：描述被改写时，这几条判据必须还在。
    """
    registry = ToolRegistry()
    desc = make_call_sub_agent(SubChatModel(), registry, ["travel"], 32000).description
    for keyword in ("USE FOR", "RULES", "多轮", "一无所知", "拼进", "自己查"):
        assert keyword in desc, f"call_sub_agent 描述丢了触发契约：{keyword!r}"
