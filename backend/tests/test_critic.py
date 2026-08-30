# tests/test_critic.py
# critic 回路的契约测试：run_critic 的 ok=true/ok=false 两分支，以及 service 层的触发条件
# （仅硬数据工具 train_ticket_query/flight_query/weather_query 才触发校验）。
import json

import pytest

from app.config import settings
from app.core.critic import run_critic
from app.services.agent_service import AgentService


class FixedLLM:
    """固定返回预设 critic JSON 的 LLM 假实现，并记录 chat（critic）调用。"""
    def __init__(self, content):
        self.content = content
        self.chat_calls = []

    async def chat(self, messages, tools=None, response_format=None):
        self.chat_calls.append(messages)
        return {"content": self.content, "tool_calls": None}

    async def complete(self, messages):
        return ""

    async def stream_chat(self, messages, tools=None):
        yield {"type": "end", "tool_calls": None}


@pytest.mark.asyncio
async def test_run_critic_ok_true():
    """ok=true 分支：所有事实可被工具结果支撑，返回空 issues。"""
    llm = FixedLLM('{"ok":true,"issues":[]}')
    r = await run_critic(llm, "天气：晴", "成都明天晴")
    assert r == {"ok": True, "issues": []}


@pytest.mark.asyncio
async def test_run_critic_ok_false():
    """ok=false 分支：返回有问题的声明列表（claim/problem/correction）。"""
    llm = FixedLLM('{"ok":false,"issues":[{"claim":"票价100元","problem":"编造票价","correction":"删除票价"}]}')
    r = await run_critic(llm, "天气：晴", "成都明天晴，票价100元")
    assert r["ok"] is False
    assert r["issues"][0]["claim"] == "票价100元"


class ScriptedStreamLLM:
    """按脚本产出 stream_chat 事件（每轮生成吐一个 turn），并记录 chat（critic）/complete（修正轮）调用。"""
    def __init__(self, stream_turns, critic_response='{"ok":true,"issues":[]}', complete_response=""):
        self.stream_turns = list(stream_turns)
        self.i = 0
        self.chat_calls = []
        self.stream_messages = []   # 记录每次 stream_chat 收到的 messages
        self.complete_calls = []    # 记录修正轮的 complete 调用
        self.critic_response = critic_response
        self.complete_response = complete_response

    async def stream_chat(self, messages, tools=None):
        self.stream_messages.append(messages)
        turn = self.stream_turns[self.i]
        self.i += 1
        for e in turn:
            yield e

    async def chat(self, messages, tools=None, response_format=None):
        self.chat_calls.append(messages)
        return {"content": self.critic_response, "tool_calls": None}

    async def complete(self, messages):
        self.complete_calls.append(messages)
        return self.complete_response


class FakeDS:
    """高德数据源假实现：返回空/固定契约结构，避免真实 MCP 网络。"""
    async def search_poi(self, keywords, city, **kw):
        return []

    async def get_weather(self, city, days):
        return [{"date": "2026-08-30", "day_weather": "晴"}]


def _make_svc(tmp_path, monkeypatch, llm):
    """构造带假数据源/假 LLM 的 service，并摘掉异步 LTM 提炼。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "svc.db"))
    svc = AgentService()
    svc.ds = FakeDS()
    svc.llm = llm

    async def noop(user_id, conv_id):
        pass
    svc._extract_facts = noop
    return svc


@pytest.mark.asyncio
async def test_critic_triggers_on_hard_data(tmp_path, monkeypatch):
    """触发条件：调用硬数据工具（weather_query）才触发 critic。"""
    llm = ScriptedStreamLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "weather_query", "arguments": '{"city":"成都"}'}}]}],
        [{"type": "content", "text": "成都明天晴"}, {"type": "end", "tool_calls": None}],
    ])
    svc = _make_svc(tmp_path, monkeypatch, llm)
    [e async for e in svc.chat_stream("u1", None, "成都天气")]
    assert len(llm.chat_calls) == 1   # weather_query 是硬数据工具，critic 被触发


@pytest.mark.asyncio
async def test_critic_skips_without_hard_data(tmp_path, monkeypatch):
    """触发条件：只调非硬数据工具（poi_search）不触发 critic。"""
    llm = ScriptedStreamLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "poi_search", "arguments": '{"keywords":"景点","city":"成都"}'}}]}],
        [{"type": "content", "text": "宽窄巷子"}, {"type": "end", "tool_calls": None}],
    ])
    svc = _make_svc(tmp_path, monkeypatch, llm)
    [e async for e in svc.chat_stream("u1", None, "成都景点")]
    assert llm.chat_calls == []   # poi_search 非硬数据，不触发 critic


@pytest.mark.asyncio
async def test_critic_corrects_answer_when_rejected(tmp_path, monkeypatch):
    """ok=false 分支：critic 拒绝后，复用第一轮工具结果，用 complete 直接生成修正答案（不重新调工具）。"""
    llm = ScriptedStreamLLM([
        # 第一轮生成：调 weather_query 后给出含编造票价的回答
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "weather_query", "arguments": '{"city":"成都"}'}}]}],
        [{"type": "content", "text": "成都明天晴，机票100元"}, {"type": "end", "tool_calls": None}],
    ], critic_response='{"ok":false,"issues":[{"claim":"机票100元","problem":"编造票价","correction":"删除票价"}]}',
       complete_response="成都明天晴")
    svc = _make_svc(tmp_path, monkeypatch, llm)
    events = [e async for e in svc.chat_stream("u1", None, "成都天气")]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conv.get_history("u1", conv_id)
    saved = json.loads(history[-1]["content"])
    assert saved["content"] == "成都明天晴"   # 修正后的回答被落库
    assert len(llm.chat_calls) == 1           # critic 调一次
    assert len(llm.complete_calls) == 1       # 修正轮用 complete（不再重新 stream_chat 调工具）
    # 修正轮的 complete 收到工具结果 + 修正要点
    correction_user = [m for m in llm.complete_calls[0] if m["role"] == "user"][-1]
    assert "原始问题：成都天气" in correction_user["content"]
    assert "修正要点" in correction_user["content"]
    assert "工具结果" in correction_user["content"]
