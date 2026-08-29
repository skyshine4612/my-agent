# tests/test_llm.py
# LLM 客户端接口契约测试：使用 FakeLLM 验证 LLMClient 抽象接口，不依赖真实 API
import pytest

from app.core.llm import LLMClient


class FakeLLM(LLMClient):
    """伪造的 LLM 客户端：固定返回预设结果，用于验证接口契约"""

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "ok", "tool_calls": None}

    async def complete(self, messages):
        return "摘要"


@pytest.mark.asyncio
async def test_llm_contract():
    """验证 chat() 返回结构符合契约：content 为字符串，tool_calls 为 None"""
    r = await FakeLLM().chat([{"role": "user", "content": "hi"}])
    assert r["content"] == "ok" and r["tool_calls"] is None


@pytest.mark.asyncio
async def test_llm_complete():
    """验证 complete() 返回纯文本字符串"""
    r = await FakeLLM().complete([{"role": "user", "content": "hi"}])
    assert r == "摘要"
