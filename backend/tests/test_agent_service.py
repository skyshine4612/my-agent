# tests/test_agent_service.py
# AgentService 装配与流式对话契约测试：
# 验证 service 装配成功、工具非空、chat_stream 用 fake LLM 跑通并产出 conversation_id/done 事件。
import json

import pytest

from app.config import settings
from app.services.agent_service import AgentService


class FakeStreamingLLM:
    """流式 LLM 假实现：stream_chat 固定吐一段文本后收尾（不调工具），chat/complete 兜底。"""
    async def stream_chat(self, messages, tools=None):
        yield {"type": "content", "text": "你好，成都"}
        yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": '{"ok":true,"issues":[]}', "tool_calls": None}

    async def complete(self, messages):
        return ""


def test_service_assembles_and_tools_non_empty(tmp_path, monkeypatch):
    """AgentService 装配成功（conv/ltm/llm/ds 就位），扁平工具注册表非空且含 call_sub_agent。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "svc.db"))
    svc = AgentService()
    assert svc.conv is not None
    assert svc.ltm is not None
    assert svc.llm is not None
    assert svc.ds is not None
    reg = svc._build_registry()
    names = reg.list_names()
    assert names, "工具注册表不应为空"
    assert "call_sub_agent" in names
    assert "poi_search" in names and "weather_query" in names


@pytest.mark.asyncio
async def test_chat_stream_flow(tmp_path, monkeypatch):
    """chat_stream 用 fake LLM 跑通：产出 conversation_id/token/done 事件，并落库结构化 assistant 消息。"""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "svc.db"))
    svc = AgentService()
    svc.llm = FakeStreamingLLM()

    async def noop(user_id, conv_id):
        pass
    svc._extract_facts = noop   # 摘掉异步 LTM 提炼，避免 fire-and-forget task 悬挂

    events = [e async for e in svc.chat_stream("u1", None, "成都三日游")]
    types = [e["type"] for e in events]
    assert "conversation_id" in types
    assert "token" in types
    assert types[-1] == "done"

    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conv.get_history("u1", conv_id)
    assert [h["role"] for h in history] == ["user", "assistant"]
    saved = json.loads(history[-1]["content"])
    assert saved["content"] == "你好，成都"
    assert saved["tools"] == []
