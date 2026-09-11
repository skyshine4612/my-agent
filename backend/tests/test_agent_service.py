# tests/test_agent_service.py
# AgentService 装配与流式对话契约测试：
# 验证 service 装配成功、工具非空、chat_stream 用 fake LLM 跑通并产出 conversation_id/done 事件。
import asyncio
import json

import pytest

from app.config import settings
from app.services.agent_service import AgentService
from app.tools import travel as travel_tools


class FakeStreamingLLM:
    """流式 LLM 假实现：stream_chat 固定吐一段文本后收尾（不调工具），chat/complete 兜底。"""

    async def stream_chat(self, messages, tools=None):
        yield {"type": "content", "text": "你好，成都"}
        yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": '{"ok":true,"issues":[]}', "tool_calls": None}

    async def complete(self, messages):
        return ""


def test_service_assembles_and_tools_non_empty():
    """AgentService 装配成功（会话历史/短期/长期记忆/llm/ds 就位），扁平工具注册表非空。

    store 的 __init__ 不做任何 IO（表结构由数据库侧建），因此这里不需要数据库 fixture。
    """
    svc = AgentService()
    assert svc.conversation_store is not None
    assert svc.short_term_memory is not None
    assert svc.long_term_memory is not None
    assert svc.llm is not None
    names = svc.registry.list_names()
    assert names, "工具注册表不应为空"
    assert "get_skill" in names
    assert "poi_search" in names and "weather_query" in names
    assert "tavily_search" in names and "tavily_extract" in names


@pytest.mark.asyncio
async def test_chat_stream_flow(user_id):
    """chat_stream 用 fake LLM 跑通：产出 conversation_id/token/done 事件，并落库结构化 assistant 消息。"""
    svc = AgentService()
    svc.llm = FakeStreamingLLM()

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop  # 摘掉异步 LTM 提炼，避免 fire-and-forget task 悬挂

    events = [e async for e in svc.chat_stream(user_id, None, "成都三日游")]
    types = [e["type"] for e in events]
    assert "conversation_id" in types
    assert "token" in types
    assert types[-1] == "done"

    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conversation_store.get_history(user_id, conv_id)
    assert [h["role"] for h in history] == ["user", "assistant"]
    saved = json.loads(history[-1]["content"])
    assert saved["content"] == "你好，成都"
    assert saved["tools"] == []
    # 短期记忆应包含本轮 user + assistant（修正 _assemble_history 滞后一轮的问题）
    records = await svc.short_term_memory.get_records(user_id, conv_id)
    assert [r["role"] for r in records] == ["user", "assistant"]
    assert records[0]["content"] == "成都三日游"
    assert records[1]["content"] == "你好，成都"


@pytest.mark.asyncio
async def test_registry_built_once_and_reused_across_requests(user_id, monkeypatch):
    """registry 在 __init__ 一次 build 并缓存，chat_stream 复用缓存，不每请求重建（不泄漏 MCP 数据源）。

    通过 spy register_travel_tools 的调用次数验证：register_travel_tools 内联 new 的
    Train/Flight MCP 数据源（其 __init__ 会创建从不 close 的 httpx 客户端）只在启动时 new 一次。
    """
    register_calls = []
    orig = travel_tools.register_travel_tools

    def spy(registry, amap_web_ds):
        register_calls.append(registry)
        return orig(registry, amap_web_ds)

    monkeypatch.setattr(travel_tools, "register_travel_tools", spy)
    svc = AgentService()
    assert len(register_calls) == 1  # __init__ 只装配一次（数据源只 new 一次）
    cached = svc.registry

    svc.llm = FakeStreamingLLM()

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    [e async for e in svc.chat_stream(user_id, None, "第一次请求")]
    [e async for e in svc.chat_stream(user_id, None, "第二次请求")]
    assert len(register_calls) == 1  # 两次 chat_stream 不再触发 register_tools
    assert svc.registry is cached  # 复用同一个 registry 实例


class SameNameStreamLLM:
    """同名工具并行的流式 LLM 假实现：第一轮同时调用两个同名 poi_search（参数不同）。"""

    def __init__(self):
        self.i = 0
        self.turns = [
            [{"type": "end", "tool_calls": [
                {"id": "call_a", "function": {"name": "poi_search", "arguments": '{"keywords":"景点A","city":"成都"}'}},
                {"id": "call_b", "function": {"name": "poi_search", "arguments": '{"keywords":"景点B","city":"成都"}'}},
            ]}],
            [{"type": "content", "text": "结果已汇总"}, {"type": "end", "tool_calls": None}],
        ]

    async def stream_chat(self, messages, tools=None):
        turn = self.turns[self.i]
        self.i += 1
        for e in turn:
            yield e

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": '{"ok":true,"issues":[]}', "tool_calls": None}

    async def complete(self, messages):
        return ""


class SlowFastRegistry:
    """同名工具按入参区分、且完成时间相反（先调用慢、后调用快）的假注册表。

    并行执行时 tool_result 按「完成先后」返回：后调用（B）先完成。若按名或按队列
    顺序配对，会把 B 的结果错配到先调用的 A；只有按 tool_call_id 配对才不串。
    """

    def list_names(self):
        return ["poi_search"]

    def to_openai_schemas(self, names):
        return []

    def label(self, name):
        return name

    async def call_raw(self, name, args):
        kw = args.get("keywords", "")
        if kw == "景点A":
            await asyncio.sleep(0.05)  # 先调用的工具慢，稍后才返回
            return "结果A"
        await asyncio.sleep(0.01)  # 后调用的工具快，先返回
        return "结果B"


@pytest.mark.asyncio
async def test_tool_result_pairs_by_call_id_same_name_parallel(user_id):
    """同名工具并行且乱序完成时，tool_result 按 tool_call_id 精确配对 summary，不串。"""
    svc = AgentService()
    svc.llm = SameNameStreamLLM()
    svc.registry = SlowFastRegistry()

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    events = [e async for e in svc.chat_stream(user_id, None, "帮我搜两个景点")]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conversation_store.get_history(user_id, conv_id)
    saved = json.loads(history[-1]["content"])
    tools = saved["tools"]
    # 两条同名工具都落库，且各自 summary 与入参一一对应（不被并发乱序串配）
    assert [t["tool"] for t in tools] == ["poi_search", "poi_search"]
    by_kw = {t["args"]["keywords"]: t["result"] for t in tools}
    assert by_kw == {"景点A": "结果A", "景点B": "结果B"}
    # SSE 事件透传 tool_call_id：前端据此按 id 精确回填 summary（同名工具并发也不串）
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    tool_results = [e for e in events if e["type"] == "tool_result"]
    assert {e["id"] for e in tool_calls} == {"call_a", "call_b"}
    assert {e["id"] for e in tool_results} == {"call_a", "call_b"}


class RecordingLLM:
    """记录 complete 调用次数的 LLM 假实现，验证增量压缩不重复调用摘要浓缩。"""

    def __init__(self, summary="累积摘要"):
        self.summary = summary
        self.complete_calls = 0

    async def complete(self, messages):
        self.complete_calls += 1
        return self.summary


@pytest.mark.asyncio
async def test_assemble_history_no_recompression(user_id, tmp_path, monkeypatch):
    """压缩物理删除早期回合：压缩后再次 assemble 不重复压缩（删除即增量，无需 marker）。"""
    monkeypatch.setattr(settings, "result_dir", str(tmp_path / "results"))
    monkeypatch.setattr(settings, "llm_context_budget", 100)  # 小预算强制早期回合压缩
    svc = AgentService()
    llm = RecordingLLM()
    svc.llm = llm
    svc.memory.llm = llm

    cid = await svc.conversation_store.create_conversation(user_id)
    # 写 4 个回合到短期记忆表（每回合 user + assistant），远超小预算，必然触发早期回合压缩
    for i in range(4):
        await svc.memory.append_turn(user_id, cid, f"用户问题{i}" + "长" * 40, f"助手回答{i}" + "长" * 40)

    # 第一次 assemble：触发压缩，摘要浓缩调用一次
    await svc.memory.assemble_history(user_id, cid)
    assert llm.complete_calls == 1

    # 第二次 assemble：早期回合已被物理删除，不应再压缩
    await svc.memory.assemble_history(user_id, cid)
    assert llm.complete_calls == 1  # 仍为 1，未重复压缩


@pytest.mark.asyncio
async def test_assemble_history_restores_structured_tool_messages(user_id, tmp_path, monkeypatch):
    """append_turn 写结构化回合后，assemble_history 从短期记忆表还原成 tool_calls + role=tool 消息。"""
    monkeypatch.setattr(settings, "result_dir", str(tmp_path / "results"))
    svc = AgentService()
    svc.llm = FakeStreamingLLM()

    cid = await svc.conversation_store.create_conversation(user_id)
    # 一轮 tool_calls（OpenAI 格式）
    rounds = [[{"id": "c1", "type": "function",
                "function": {"name": "菜谱做法", "arguments": '{"name": "宫保鸡丁"}'}}]]
    await svc.memory.append_turn(user_id, cid, "第一天详细菜谱", "第一天做法", rounds)

    recent, summary = await svc.memory.assemble_history(user_id, cid)

    # recent 结构化：user + assistant(tool_calls) + tool + assistant(最终答案)
    assert [m["role"] for m in recent] == ["user", "assistant", "tool", "assistant"]
    assert recent[1]["tool_calls"][0]["function"]["name"] == "菜谱做法"
    assert "宫保鸡丁" in recent[1]["tool_calls"][0]["function"]["arguments"]
    assert recent[2]["tool_call_id"] == "c1"
    assert recent[2]["content"] == ""  # 不存 result，占位空串
    assert recent[3]["content"] == "第一天做法"

    # 短期记忆表存结构化记录（tool 占位不落表，仅 user + assistant(tool_calls) + assistant 答案）
    records = await svc.short_term_memory.get_records(user_id, cid)
    assert [r["role"] for r in records] == ["user", "assistant", "assistant"]
    assert records[2]["content"] == "第一天做法"


class SlowStreamingLLM:
    """慢速流式 LLM：stream_chat 先 sleep 再吐文本，模拟真实生成耗时。

    用于验证「客户端在生成中途刷新断开 SSE，后端仍应继续跑完整轮并落库」。
    """

    async def stream_chat(self, messages, tools=None):
        await asyncio.sleep(0.05)
        yield {"type": "content", "text": "你好，成都"}
        yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": '{"ok":true,"issues":[]}', "tool_calls": None}

    async def complete(self, messages):
        return ""


@pytest.mark.asyncio
async def test_disconnect_still_persists_answer(user_id):
    """客户端生成中途断开（不消费完 SSE），后端仍应跑完整轮并落库 assistant 答案。

    模拟刷新：消费到 conversation_id + 下一个事件后 aclose（= 断开 SSE），
    然后轮询等待后台落库，断言历史里出现 assistant 消息。
    """
    svc = AgentService()
    svc.llm = SlowStreamingLLM()

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    agen = svc.chat_stream(user_id, None, "成都三日游")
    # 消费 conversation_id 事件，拿到会话 id
    first = await agen.__anext__()
    conv_id = first["conversation_id"]
    # 再消费一个事件，确保 worker 已启动并进入生成阶段
    await agen.__anext__()
    # 模拟刷新：关闭生成器（= 客户端断开 SSE），后台 worker 应继续跑完并落库
    await agen.aclose()

    # 轮询等待后台 worker 完成落库
    history = []
    for _ in range(100):
        history = await svc.conversation_store.get_history(user_id, conv_id)
        if len(history) >= 2:
            break
        await asyncio.sleep(0.05)
    assert [h["role"] for h in history] == ["user", "assistant"]
    saved = json.loads(history[-1]["content"])
    assert saved["content"] == "你好，成都"
