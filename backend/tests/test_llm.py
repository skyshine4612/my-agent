# tests/test_llm.py
# LLM 客户端接口契约测试：使用 FakeLLM 验证 LLMClient 抽象接口，不依赖真实 API
from types import SimpleNamespace

import pytest

from app.core.llm import LLMClient, OpenAICompatLLM


class FakeLLM(LLMClient):
    """伪造的 LLM 客户端：固定返回预设结果，用于验证接口契约"""

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "ok", "tool_calls": None}

    async def complete(self, messages):
        return "摘要"

    async def stream_chat(self, messages, tools=None):
        # 流式契约：先产出两个 content 增量，再以 end 收尾（无工具调用）
        yield {"type": "content", "text": "你"}
        yield {"type": "content", "text": "好"}
        yield {"type": "end", "tool_calls": None}


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


@pytest.mark.asyncio
async def test_stream_chat_content_and_end():
    """验证 stream_chat 契约：按序产出 content 增量，最终以 end 事件（tool_calls=None）收尾"""
    events = [e async for e in FakeLLM().stream_chat([{"role": "user", "content": "hi"}])]
    assert events == [
        {"type": "content", "text": "你"},
        {"type": "content", "text": "好"},
        {"type": "end", "tool_calls": None},
    ]


def _tc(index, id=None, name=None, arguments=None):
    """构造模拟的流式工具调用分片（对应 delta.tool_calls 里的每个元素）"""
    return SimpleNamespace(index=index, id=id,
                           function=SimpleNamespace(name=name, arguments=arguments))


def _chunk(content=None, tool_calls=None):
    """构造模拟的流式 chunk（含 choices[0].delta）"""
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))])


class StubCompletions:
    """模拟 client.chat.completions.create：返回预设 chunk 序列的异步迭代器"""

    def __init__(self, chunks):
        self.chunks = chunks

    async def create(self, **kw):
        # 校验 stream=True 已开启，并返回异步生成器供 async for 消费
        assert kw.get("stream") is True

        async def gen():
            for c in self.chunks:
                yield c

        return gen()


class StubClient:
    """模拟 OpenAI 客户端：chat.completions 指向 StubCompletions"""

    def __init__(self, chunks):
        self.chat = SimpleNamespace(completions=StubCompletions(chunks))


@pytest.mark.asyncio
async def test_stream_chat_merges_tool_calls_by_index():
    """验证 OpenAICompatLLM.stream_chat：content 增量按序产出，tool_calls 按 index 合并成 chat() 形状"""
    llm = OpenAICompatLLM("http://127.0.0.1", "k", "m")
    llm.client = StubClient([
        _chunk(content="开始"),
        _chunk(tool_calls=[_tc(0, id="call_0", name="weather", arguments='{"ci')]),
        _chunk(tool_calls=[_tc(0, arguments='ty":"成都"}'), _tc(1, id="call_1", name="route")]),
        _chunk(tool_calls=[_tc(1, arguments='{"from":"成都"}')]),
        _chunk(),  # 收尾 chunk：无 content 无 tool_calls
    ])
    events = [e async for e in llm.stream_chat([])]
    content = "".join(e["text"] for e in events if e["type"] == "content")
    end = [e for e in events if e["type"] == "end"][0]
    assert content == "开始"
    # 两个工具调用，按 index 升序排列，形状与 chat() 的 model_dump 一致（含 "type":"function"）
    assert end["tool_calls"] == [
        {"id": "call_0", "type": "function", "function": {"name": "weather", "arguments": '{"city":"成都"}'}},
        {"id": "call_1", "type": "function", "function": {"name": "route", "arguments": '{"from":"成都"}'}},
    ]
