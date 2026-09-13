# tests/test_agent_service.py
# AgentService 装配与流式对话契约测试：
# 验证 service 装配成功、工具非空、chat_stream 用 fake LLM 跑通并产出 conversation_id/done 事件。
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

from app.config import settings
from app.services.agent_service import AgentService
from app.tools import travel as travel_tools


def _lc_tool_call(tc):
    """OpenAI 形状的 tool_call → langchain 形状（arguments JSON 串解析成 args dict）。"""
    return {"id": tc["id"], "name": tc["function"]["name"],
            "args": json.loads(tc["function"]["arguments"] or "{}")}


class EventScriptChatModel(BaseChatModel):
    """把事件式假 LLM 的 stream_chat 脚本适配成 langchain 模型。

    content 事件拼成答案文本、end 事件的 tool_calls 转 langchain 形状；
    主循环、critic 与记忆蒸馏共用这一个模型实例（同一份脚本）。
    """

    llm: Any = None

    @property
    def _llm_type(self):
        return "event-script"

    def bind_tools(self, tools, **kwargs):
        # 覆盖为原样返回自身：create_agent 在调用期才 bind_tools，裸 BaseChatModel 会抛 NotImplementedError
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        # 事件脚本是 async generator（stream_chat），只能走异步路径
        raise NotImplementedError("EventScriptChatModel 只支持异步调用")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        events = [e async for e in self.llm.stream_chat(convert_to_openai_messages(messages))]
        content = "".join(e.get("text", "") for e in events if e.get("type") == "content")
        calls = next((e.get("tool_calls") for e in events if e.get("type") == "end"), None)
        return ChatResult(generations=[ChatGeneration(
            message=AIMessage(content=content, tool_calls=[_lc_tool_call(tc) for tc in (calls or [])]))])


def _StubTool(name):
    """占位工具条目（与 ToolRegistry.get 返回的 Tool 同形）：fn 是同名 langchain tool，
    仅供 create_agent 装配 schema —— 执行被 middleware 短路，不会真跑。"""

    @tool(name)
    def _stub() -> str:
        """占位工具。"""
        return ""

    return SimpleNamespace(fn=_stub)


class FakeStreamingLLM:
    """流式 LLM 假实现：stream_chat 固定吐一段文本后收尾（不调工具）。"""

    async def stream_chat(self, messages, tools=None):
        yield {"type": "content", "text": "你好，成都"}
        yield {"type": "end", "tool_calls": None}


def test_service_assembles_and_tools_non_empty():
    """AgentService 装配成功（会话历史/短期/长期记忆/chat_model/ds 就位），扁平工具注册表非空。

    store 的 __init__ 不做任何 IO（表结构由数据库侧建），因此这里不需要数据库 fixture。
    """
    svc = AgentService()
    assert svc.conversation_store is not None
    assert svc.short_term_memory is not None
    assert svc.long_term_memory is not None
    assert svc.chat_model is not None
    names = svc.registry.list_names()
    assert names, "工具注册表不应为空"
    assert "get_skill" in names
    assert "poi_search" in names and "weather_query" in names
    assert "tavily_search" in names and "tavily_extract" in names


@pytest.mark.asyncio
async def test_chat_stream_flow(user_id):
    """chat_stream 用 fake LLM 跑通：产出 conversation_id/token/done 事件，并落库结构化 assistant 消息。"""
    svc = AgentService()
    svc.chat_model = EventScriptChatModel(llm=FakeStreamingLLM())

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop  # 摘掉异步 LTM 提炼，避免 fire-and-forget task 悬挂

    events = [e async for e in svc.chat_stream(user_id, None, "成都三日游")]
    types = [e["type"] for e in events]
    assert "conversation_id" in types
    assert "token" in types
    assert types[-1] == "done"
    # 最终答案按小块下发：拼接后必须还原成完整答案（分块在这一层做，run_stream 只把答案作为返回值交出）
    assert "".join(e["content"] for e in events if e["type"] == "token") == "你好，成都"

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
async def test_chat_stream_records_attachments_on_user_message(user_id):
    """用户消息带上附件一起落库：前端刷新、切会话后仍能显示这条消息传了哪个文件。

    标题仍取纯文本——别把附件 JSON 吃进会话标题。
    """
    svc = AgentService()
    svc.chat_model = EventScriptChatModel(llm=FakeStreamingLLM())

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    files = [{"file_id": "f1", "name": "大文件.xlsx", "size": 5748559}]
    events = [e async for e in svc.chat_stream(user_id, None, "帮我把excel文件做下脱敏", files=files)]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")

    history = await svc.conversation_store.get_history(user_id, conv_id)
    saved = json.loads(history[0]["content"])
    assert saved["content"] == "帮我把excel文件做下脱敏"
    assert saved["files"] == files

    convs = await svc.conversation_store.list_conversations(user_id)
    assert next(c for c in convs if c["id"] == conv_id)["title"] == "帮我把excel文件做下脱敏"


@pytest.mark.asyncio
async def test_chat_stream_without_files_keeps_user_message_plain(user_id):
    """没带附件时用户消息仍是纯文本——历史里已存的旧消息是纯文本，两种格式前端都要吃得下。"""
    svc = AgentService()
    svc.chat_model = EventScriptChatModel(llm=FakeStreamingLLM())

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    events = [e async for e in svc.chat_stream(user_id, None, "成都三日游")]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conversation_store.get_history(user_id, conv_id)
    assert history[0]["content"] == "成都三日游"
    # 短期记忆同样原样，别把「无附件」也渲染成一块空壳
    records = await svc.short_term_memory.get_records(user_id, conv_id)
    assert records[0]["content"] == "成都三日游"


class CapturingStreamLLM:
    """记录每次调用看到的 messages，供断言「模型实际收到的是什么」。"""

    def __init__(self):
        self.calls = []

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        yield {"type": "content", "text": "好的"}
        yield {"type": "end", "tool_calls": None}


@pytest.mark.asyncio
async def test_attachment_block_reaches_model_and_survives_into_the_next_turn(user_id):
    """附件折进 user 正文的完整口径：本轮模型收到它、短期记忆存下同一份、下一轮组装历史时还在。

    三处任一漂移都断链——模型这轮看到的名字与它下一轮在记忆里看到的对不上，追问「刚才那个
    文件」时就会去找一个不存在的名字。所以断言的是「同一份文本」，而不是「含某段文字」。
    """
    svc = AgentService()
    llm = CapturingStreamLLM()
    svc.chat_model = EventScriptChatModel(llm=llm)

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    expected = "帮我把excel文件做下脱敏\n[本条消息携带的文件]\n- 大文件.xlsx  5.5MB"
    files = [{"file_id": "f1", "name": "大文件.xlsx", "size": 5748559}]
    events = [e async for e in svc.chat_stream(user_id, None, "帮我把excel文件做下脱敏", files=files)]
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")

    seen_user = [m for m in llm.calls[0] if m.get("role") == "user"]
    assert seen_user[-1]["content"] == expected
    records = await svc.short_term_memory.get_records(user_id, conv_id)
    assert records[0]["role"] == "user"
    assert records[0]["content"] == expected
    # 下一轮组装出的历史里还带着它——「跨轮指代有事实可依」的实测口径
    history, _ = await svc.memory.assemble_history(user_id, conv_id)
    assert history[0]["content"] == expected


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

    svc.chat_model = EventScriptChatModel(llm=FakeStreamingLLM())

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


class SlowFastRegistry:
    """同名工具按入参区分、且完成时间相反（先调用慢、后调用快）的假注册表。

    并行执行时 tool_result 按「完成先后」返回：后调用（B）先完成。若按名或按队列
    顺序配对，会把 B 的结果错配到先调用的 A；只有按 tool_call_id 配对才不串。
    """

    def list_names(self):
        return ["poi_search"]

    def get(self, name):
        return _StubTool(name)

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
    svc.chat_model = EventScriptChatModel(llm=SameNameStreamLLM())
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


class ImageToolLLM:
    """第一轮调一次 search_knowledge，第二轮收口。"""

    def __init__(self):
        self.i = 0
        self.turns = [
            [{"type": "end", "tool_calls": [
                {"id": "call_k", "function": {"name": "search_knowledge",
                                              "arguments": '{"query":"西湖"}'}}]}],
            [{"type": "content", "text": "西湖很好"}, {"type": "end", "tool_calls": None}],
        ]

    async def stream_chat(self, messages, tools=None):
        turn = self.turns[self.i]
        self.i += 1
        for e in turn:
            yield e


class ImageRegistry:
    """call_raw 返回 search_knowledge 那种带侧信道的结构化结果。"""

    def list_names(self):
        return ["search_knowledge"]

    def get(self, name):
        return _StubTool(name)

    def label(self, name):
        return "检索知识库"

    async def call_raw(self, name, args):
        return {"hits": [{"content": "西湖很美"}],
                "images": [{"key": "a/b/0.jpg", "desc": "西湖"}]}


@pytest.mark.asyncio
async def test_images_reach_both_sse_and_history(user_id):
    """图片在事件构造、落库、SSE 三处各有一份显式白名单，漏哪一处都不报错只静默失效：
    只改事件构造的话前端收得到、落库丢了，刷新一次缩略图就没了。两处下游一起断言。"""
    svc = AgentService()
    svc.chat_model = EventScriptChatModel(llm=ImageToolLLM())
    svc.registry = ImageRegistry()

    async def noop(user_id, conv_id):
        pass

    svc.memory.extract_facts = noop

    events = [e async for e in svc.chat_stream(user_id, None, "西湖怎么玩")]
    imgs = [{"key": "a/b/0.jpg", "desc": "西湖"}]
    results = [e for e in events if e["type"] == "tool_result"]
    assert [e.get("images") for e in results] == [imgs]

    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation_id")
    history = await svc.conversation_store.get_history(user_id, conv_id)
    saved = json.loads(history[-1]["content"])
    assert saved["tools"][0]["images"] == imgs


class RecordingLLM:
    """记录蒸馏调用次数的假模型，验证增量压缩不重复调用摘要浓缩。"""

    def __init__(self, summary="累积摘要"):
        self.summary = summary
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return AIMessage(content=self.summary)


@pytest.mark.asyncio
async def test_assemble_history_no_recompression(user_id, tmp_path, monkeypatch):
    """压缩物理删除早期回合：压缩后再次 assemble 不重复压缩（删除即增量，无需 marker）。"""
    monkeypatch.setattr(settings, "result_dir", str(tmp_path / "results"))
    monkeypatch.setattr(settings, "llm_context_budget", 100)  # 小预算强制早期回合压缩
    svc = AgentService()
    llm = RecordingLLM()
    svc.memory.model = llm

    cid = await svc.conversation_store.create_conversation(user_id)
    # 写 4 个回合到短期记忆表（每回合 user + assistant），远超小预算，必然触发早期回合压缩
    for i in range(4):
        await svc.memory.append_turn(user_id, cid, f"用户问题{i}" + "长" * 40, f"助手回答{i}" + "长" * 40)

    # 第一次 assemble：触发压缩，摘要浓缩调用一次
    await svc.memory.assemble_history(user_id, cid)
    assert llm.calls == 1

    # 第二次 assemble：早期回合已被物理删除，不应再压缩
    await svc.memory.assemble_history(user_id, cid)
    assert llm.calls == 1  # 仍为 1，未重复压缩


@pytest.mark.asyncio
async def test_assemble_history_restores_structured_tool_messages(user_id, tmp_path, monkeypatch):
    """append_turn 写结构化回合后，assemble_history 从短期记忆表还原成 tool_calls + role=tool 消息。"""
    monkeypatch.setattr(settings, "result_dir", str(tmp_path / "results"))
    svc = AgentService()

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


@pytest.mark.asyncio
async def test_disconnect_still_persists_answer(user_id):
    """客户端生成中途断开（不消费完 SSE），后端仍应跑完整轮并落库 assistant 答案。

    模拟刷新：消费到 conversation_id + 下一个事件后 aclose（= 断开 SSE），
    然后轮询等待后台落库，断言历史里出现 assistant 消息。
    """
    svc = AgentService()
    svc.chat_model = EventScriptChatModel(llm=SlowStreamingLLM())

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
