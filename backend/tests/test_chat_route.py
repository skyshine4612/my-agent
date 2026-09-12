# chat 路由测试：附件从 HTTP 请求体走到用户消息落库的全链路。
# 不连库、不调 LLM——会话归属、上下文装配、生成都换成替身，只留路由与 chat_stream 的接线。
import json

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.main import app
from app.services.agent_service import service


@pytest.fixture
def client(monkeypatch):
    captured: list[dict] = []

    async def _belongs_to(user_id, conv_id):
        return conv_id == "c1"

    async def _create_conversation(user_id, title=None):
        return "c1"

    async def _update_title(user_id, conv_id, title):
        captured.append({"title": title})

    async def _add_message(user_id, conv_id, role, content):
        captured.append({"role": role, "content": content})

    # 上下文装配与生成都摘掉：这里只验请求体里的附件有没有被带进落库的用户消息
    async def _prepare_context(user_id, conv_id, message, *a, **kw):
        return [], "", None

    async def _stream_agent_reply(agent, history, conv_id, message, state, summary_hint=""):
        state.answer = "好的"
        if False:
            yield

    async def _append_turn(*a, **kw):
        pass

    async def _extract_facts(*a, **kw):
        pass

    monkeypatch.setattr(service.conversation_store, "belongs_to", _belongs_to)
    monkeypatch.setattr(service.conversation_store, "create_conversation", _create_conversation)
    monkeypatch.setattr(service.conversation_store, "update_title", _update_title)
    monkeypatch.setattr(service.conversation_store, "add_message", _add_message)
    monkeypatch.setattr(service, "_prepare_context", _prepare_context)
    monkeypatch.setattr(service, "_stream_agent_reply", _stream_agent_reply)
    monkeypatch.setattr(service.memory, "append_turn", _append_turn)
    monkeypatch.setattr(service.memory, "extract_facts", _extract_facts)

    app.dependency_overrides[get_current_user] = lambda: "u-test"
    yield TestClient(app), captured
    app.dependency_overrides.clear()


def _user_message(captured):
    return next(c["content"] for c in captured if c.get("role") == "user")


def test_route_stores_attachments_with_user_message(client):
    """附件随消息发来，落库时要和文本一起存进用户消息——只存文本，前端刷新后就再也看不到传过什么。"""
    c, captured = client
    files = [{"file_id": "f1", "name": "大文件.xlsx", "size": 5748559}]
    r = c.post("/api/agent/chat", json={"message": "帮我脱敏", "files": files})
    assert r.status_code == 200
    stored = json.loads(_user_message(captured))
    assert stored["content"] == "帮我脱敏"
    assert stored["files"] == files


def test_route_without_files_stores_plain_text(client):
    """不带附件仍是纯文本：历史里已存的旧消息就是纯文本，两种格式前端都能解析。"""
    c, captured = client
    r = c.post("/api/agent/chat", json={"message": "帮我脱敏"})
    assert r.status_code == 200
    assert _user_message(captured) == "帮我脱敏"


def test_route_keeps_plain_text_as_conversation_title(client):
    """会话标题取纯文本前 20 字，别把附件 JSON 吃进去。"""
    c, captured = client
    r = c.post("/api/agent/chat", json={"message": "帮我脱敏",
                                        "files": [{"file_id": "f1", "name": "a.xlsx", "size": 1}]})
    assert r.status_code == 200
    assert next(c["title"] for c in captured if "title" in c) == "帮我脱敏"
