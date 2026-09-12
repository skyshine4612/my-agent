# tests/test_critic.py
# critic 回路契约测试：run_critic 的 ok=true/ok=false、多轮带工具（read_file/grep）、限轮 fail-open，
# 以及 service 层的触发条件（仅硬数据工具触发）与修正答案流程。
import json
import logging

import pytest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.config import settings
from app.core.critic import CriticVerdict, run_critic
from app.datasource.uapis import UapisDataSource
from app.services.agent_service import AgentService
from app.tools.file import make_grep, make_read_file


class ScriptedChatModel(BaseChatModel):
    """按脚本逐次返回预设 AIMessage 的 langchain 假模型（critic 子图测试用）。

    bind_tools 覆盖为原样返回自身：create_agent 在**调用期**才会 bind_tools（构建期不调用），
    裸 BaseChatModel 会抛 NotImplementedError——所以测试必须真跑 ainvoke，只测构造是空转。
    """

    responses: list = []    # 依次返回的消息（超出脚本范围会 IndexError，用来暴露多调）
    idx: int = 0            # 已返回条数 = 模型被调用次数
    bound_tools: list = []  # 每次 bind_tools 收到的工具名列表，用来断言「附了哪些工具」

    @property
    def _llm_type(self):
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        self.bound_tools.append([t.name for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        msg = self.responses[self.idx]
        self.idx += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


class BoomChatModel(ScriptedChatModel):
    """一调用就抛异常的假模型：模拟 401 / 网络抖动等真实 provider 调用失败。"""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("模拟 LLM 调用失败")


def _tool_call(call_id, name, args):
    return {"id": call_id, "name": name, "args": args}


def _verdict_call(ok, issues=None, call_id="v1"):
    """critic 下结论用的结构化输出 tool_call：工具名就是 CriticVerdict 模型名。"""
    return _tool_call(call_id, "CriticVerdict", {"ok": ok, "issues": issues or []})


def _verdict_model(ok=True, issues=None):
    """只下一个结论就结束的假模型（单次 critic 测试用）。"""
    return ScriptedChatModel(responses=[AIMessage(content="", tool_calls=[_verdict_call(ok, issues)])])


class _FakeStore:
    """假 ToolResultStore：记录 read/search 调用并返回固定片段。"""

    def __init__(self):
        self.reads = []
        self.searches = []

    async def read(self, path, offset, limit, user_id=None):
        self.reads.append((path, offset, limit))
        return {"content": "70元", "next_offset": 3, "total": 3, "eof": True}

    async def search(self, path, pattern, user_id=None):
        self.searches.append((pattern, path))
        return {"matches": [], "count": 0}


@pytest.mark.asyncio
async def test_run_critic_ok_true():
    """ok=true 分支：所有事实可被工具结果支撑，返回空 issues。"""
    model = _verdict_model(ok=True)
    r = await run_critic(model, "天气：晴", "成都明天晴")
    assert r == {"ok": True, "issues": []}


@pytest.mark.asyncio
async def test_run_critic_ok_false():
    """ok=false 分支：返回有问题的声明列表（claim/problem/correction）。"""
    model = _verdict_model(ok=False, issues=[{"claim": "票价100元", "problem": "编造票价", "correction": "删除票价"}])
    r = await run_critic(model, "天气：晴", "成都明天晴，票价100元")
    assert r["ok"] is False
    assert r["issues"][0]["claim"] == "票价100元"


@pytest.mark.asyncio
async def test_critic_multi_turn_reads_file(caplog):
    """critic 多轮：第一轮调用 read_file 读完整结果，第二轮下结论 ok=false + issue。"""
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[
            _tool_call("c1", "read_file", {"path": "conv1/a.txt", "offset": 0, "limit": 3500})]),
        AIMessage(content="", tool_calls=[_verdict_call(
            False, [{"claim": "票价100元", "problem": "工具结果是70元", "correction": "改为70元"}])]),
    ])
    store = _FakeStore()

    with caplog.at_level(logging.INFO, logger="app.core.critic"):
        verdict = await run_critic(model, "工具结果：票价70元", "票价100元",
                                   read_file_tool=make_read_file(store), grep_tool=make_grep(store), max_iter=3)
    assert verdict["ok"] is False
    assert len(verdict["issues"]) == 1
    assert store.reads == [("conv1/a.txt", 0, 3500)]  # critic 确实调用了 read_file 读完整结果
    assert model.bound_tools[0] == ["read_file", "grep", "CriticVerdict"]  # 附了 read_file/grep 工具 schema
    # 读了哪个文件必须留在日志里（由 on_tool_start 回调打的），否则误判无从排查
    assert "[critic] 调用工具 read_file，参数" in caplog.text
    assert "conv1/a.txt" in caplog.text


@pytest.mark.asyncio
async def test_critic_single_call_without_read_fn():
    """无 read_file_tool 时退化为单次调用，不附 read_file/grep 工具。"""
    model = _verdict_model(ok=True)
    verdict = await run_critic(model, "工具结果：晴", "天气晴")
    assert verdict == {"ok": True, "issues": []}
    assert model.idx == 1
    assert model.bound_tools == [["CriticVerdict"]]


@pytest.mark.asyncio
async def test_critic_fail_open_on_max_iter(caplog):
    """critic 每轮都在读、不收敛时，达到 max_iter 上限 fail-open（ok=True）且不再无限调工具。"""
    model = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[_tool_call(f"c{i}", "read_file", {"path": "a.txt"})])
        for i in range(10)
    ])
    store = _FakeStore()

    with caplog.at_level(logging.INFO, logger="app.core.critic"):
        verdict = await run_critic(model, "工具结果：x", "答案",
                                   read_file_tool=make_read_file(store), grep_tool=make_grep(store), max_iter=3)
    assert verdict == {"ok": True, "issues": []}
    assert model.idx == 3  # 触顶即停：轮数映射到 recursion_limit，不会无限调工具
    # 超轮数是「校验主动放弃」，必须与「校验器故障」用不同日志区分
    assert "[critic] 达到 3 轮上限未收敛，按通过处理" in caplog.text
    assert "校验调用失败" not in caplog.text


@pytest.mark.asyncio
async def test_critic_fail_open_on_model_error(caplog):
    """LLM 调用失败（401 / 网络抖动）时 fail-open（ok=True），不因校验器故障中断主流程。"""
    store = _FakeStore()

    with caplog.at_level(logging.INFO, logger="app.core.critic"):
        verdict = await run_critic(BoomChatModel(), "工具结果：晴", "天气晴",
                                   read_file_tool=make_read_file(store), grep_tool=make_grep(store))
    assert verdict == {"ok": True, "issues": []}
    assert "校验调用失败，跳过事实校验" in caplog.text
    assert "轮上限未收敛" not in caplog.text


def test_critic_verdict_docstring_becomes_tool_description():
    """CriticVerdict 的 docstring 会成为结构化输出工具的 description。

    为空则工具没有任何说明，而 create_agent 在有结构化输出工具时强制 tool_choice="any"，
    模型只能靠猜该调哪个工具；猜错即 structured_response=None → 走 fail-open 静默恒判通过。
    """
    assert CriticVerdict.__doc__ and CriticVerdict.__doc__.strip()
    spec = ToolStrategy(schema=CriticVerdict).schema_specs[0]
    assert spec.description.strip() == CriticVerdict.__doc__.strip()


@pytest.mark.asyncio
async def test_critic_fail_open_when_no_verdict():
    """模型没按契约下结论（只回文本、没调结构化输出工具）时，fail-open（ok=True）。"""
    model = ScriptedChatModel(responses=[AIMessage(content="我认为没有问题")])
    verdict = await run_critic(model, "工具结果：晴", "天气晴")
    assert verdict == {"ok": True, "issues": []}


@pytest.mark.asyncio
async def test_critic_filters_empty_correction():
    """correction 为空的 issue 判为误报：不触发修正，整体按通过处理。"""
    model = _verdict_model(ok=False, issues=[{"claim": "票价100元", "problem": "疑似编造", "correction": ""}])
    verdict = await run_critic(model, "工具结果：票价70元", "票价100元")
    assert verdict == {"ok": True, "issues": []}


class ScriptedStreamLLM:
    """按脚本产出 stream_chat 事件（每轮生成吐一个 turn），并记录 complete（修正轮）调用。"""

    def __init__(self, stream_turns, complete_response=""):
        self.stream_turns = list(stream_turns)
        self.i = 0
        self.stream_messages = []  # 记录每次 stream_chat 收到的 messages
        self.complete_calls = []  # 记录修正轮的 complete 调用
        self.complete_response = complete_response

    async def stream_chat(self, messages, tools=None):
        self.stream_messages.append(messages)
        turn = self.stream_turns[self.i]
        self.i += 1
        for e in turn:
            yield e

    async def complete(self, messages):
        self.complete_calls.append(messages)
        return self.complete_response


def _make_svc(tmp_path, monkeypatch, llm, chat_model=None):
    """构造带假 LLM 的 service，并摘掉异步 LTM 提炼、mock 天气数据源避免真实网络。

    数据库由 db fixture 提供（连 agent_test），result_dir 仍隔离到 tmp_path。
    chat_model 缺省给「恒定判通过」的假模型，避免 critic 真调 DashScope。
    """
    monkeypatch.setattr(settings, "result_dir", str(tmp_path / "results"))
    # mock 天气数据源：返回短数据，避免真实 UAPIS 网络调用，也避免大结果触发短期记忆的 LLM 精炼
    async def fake_weather(self, city, days=7):
        return [{"date": "2026-09-01", "day_weather": "晴", "night_weather": "晴", "day_temp": 30, "night_temp": 20}]
    monkeypatch.setattr(UapisDataSource, "get_weather", fake_weather)
    svc = AgentService()
    svc.llm = llm
    svc.chat_model = chat_model if chat_model is not None else _verdict_model(ok=True)

    async def noop(user_id, conv_id):
        pass

    svc._extract_facts = noop
    return svc


@pytest.mark.asyncio
async def test_critic_triggers_on_hard_data(user_id, tmp_path, monkeypatch):
    """触发条件：调用硬数据工具（weather_query）才触发 critic。"""
    llm = ScriptedStreamLLM([
        [{"type": "end",
          "tool_calls": [{"id": "1", "function": {"name": "weather_query", "arguments": '{"city":"成都"}'}}]}],
        [{"type": "content", "text": "成都明天晴"}, {"type": "end", "tool_calls": None}],
    ])
    chat_model = _verdict_model(ok=True)
    svc = _make_svc(tmp_path, monkeypatch, llm, chat_model)
    [e async for e in svc.chat_stream(user_id, None, "成都天气")]
    assert chat_model.idx == 1  # weather_query 是硬数据工具，critic 被触发


@pytest.mark.asyncio
async def test_critic_skips_without_hard_data(user_id, tmp_path, monkeypatch):
    """触发条件：只调非硬数据工具（poi_search）不触发 critic。"""
    llm = ScriptedStreamLLM([
        [{"type": "end", "tool_calls": [
            {"id": "1", "function": {"name": "poi_search", "arguments": '{"keywords":"景点","city":"成都"}'}}]}],
        [{"type": "content", "text": "宽窄巷子"}, {"type": "end", "tool_calls": None}],
    ])
    chat_model = _verdict_model(ok=True)
    svc = _make_svc(tmp_path, monkeypatch, llm, chat_model)
    [e async for e in svc.chat_stream(user_id, None, "成都景点")]
    assert chat_model.idx == 0  # poi_search 非硬数据，不触发 critic


@pytest.mark.asyncio
async def test_critic_corrects_answer_when_rejected(user_id, tmp_path, monkeypatch):
    """ok=false 分支：critic 拒绝后，复用第一轮工具结果，用 complete 直接生成修正答案（不重新调工具）。"""
    llm = ScriptedStreamLLM([
        # 第一轮生成：调 weather_query 后给出含编造票价的回答
        [{"type": "end",
          "tool_calls": [{"id": "1", "function": {"name": "weather_query", "arguments": '{"city":"成都"}'}}]}],
        [{"type": "content", "text": "成都明天晴，机票100元"}, {"type": "end", "tool_calls": None}],
    ], complete_response="成都明天晴")
    chat_model = _verdict_model(
        ok=False, issues=[{"claim": "机票100元", "problem": "编造票价", "correction": "删除票价"}])
    svc = _make_svc(tmp_path, monkeypatch, llm, chat_model)
    events = [e async for e in svc.chat_stream(user_id, None, "成都天气")]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conversation_store.get_history(user_id, conv_id)
    saved = json.loads(history[-1]["content"])
    assert saved["content"] == "成都明天晴"  # 修正后的回答被落库
    assert chat_model.idx == 1  # critic 调一次
    assert len(llm.complete_calls) == 1  # 修正轮用 complete（不再重新 stream_chat 调工具）
    # 修正轮的 complete 收到工具结果 + 修正要点
    correction_user = [m for m in llm.complete_calls[0] if m["role"] == "user"][-1]
    assert "原始问题：成都天气" in correction_user["content"]
    assert "修正要点" in correction_user["content"]
    assert "工具结果" in correction_user["content"]
