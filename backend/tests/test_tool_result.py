# tests/test_tool_result.py
# 地址索引契约测试：ToolResultStore 写文件/读/搜/删目录，read_file/grep 工具，run_stream spill 写文件。
import re

import pytest

from app.core.agent import Agent
from app.core.context import current_user_id
from app.core.memory import ToolResultStore
from app.core.registry import ToolRegistry
from app.tools.file import register_file_tools


class FakeRegistry:
    """工具注册表假实现：call_raw 返回预设结果。"""

    def __init__(self, r):
        self.r = r
        self.called = []

    async def call_raw(self, name, args):
        self.called.append((name, args))
        return self.r[name]

    def to_openai_schemas(self, names):
        return []

    def label(self, name):
        return name


class StreamingLLM:
    """按脚本产出流式事件的 LLM 假实现。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.i = 0
        self.calls = []

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        turn = self.turns[self.i]
        self.i += 1
        for e in turn:
            yield e

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "", "tool_calls": None}

    async def complete(self, messages):
        return ""


@pytest.mark.asyncio
async def test_store_write_and_read(tmp_path):
    """ToolResultStore：写文件返回相对路径，按 path + offset 读片段。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel = await store.write("conv1", "u1", "abcdefghij")
    seg = await store.read(rel, 2, 3, user_id="u1")
    assert seg["content"] == "cde"
    assert seg["next_offset"] == 5
    assert seg["total"] == 10
    assert seg["eof"] is False
    tail = await store.read(rel, 8, 5, user_id="u1")
    assert tail["content"] == "ij"
    assert tail["eof"] is True


@pytest.mark.asyncio
async def test_store_user_isolation_and_path_traversal(tmp_path):
    """ToolResultStore：跨 user_id 读不到，路径穿越被拒绝，文件不存在返回 None。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel = await store.write("conv1", "u1", "secret")
    assert await store.read(rel, 0, 10, user_id="u2") is None  # 跨用户
    assert await store.read("../x.txt", 0, 10, user_id="u1") is None  # 路径穿越
    assert await store.read("nonexistent/x.txt", 0, 10, user_id="u1") is None  # 不存在


@pytest.mark.asyncio
async def test_store_search(tmp_path):
    """ToolResultStore：正则搜返回命中位置与上下文片段。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel = await store.write("conv1", "u1", "AAAMU5160BBB CCCMU5160DDD")
    res = await store.search(rel, "MU5160", user_id="u1")
    assert res["count"] == 2
    assert res["matches"][0]["match"] == "MU5160"
    assert "offset" in res["matches"][0] and "context" in res["matches"][0]


@pytest.mark.asyncio
async def test_store_delete_conversation(tmp_path):
    """ToolResultStore：delete_conversation 只删指定会话目录，不影响其他会话。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel1 = await store.write("conv1", "u1", "aaa")
    rel2 = await store.write("conv2", "u1", "bbb")
    await store.delete_conversation("conv1", "u1")
    assert await store.read(rel1, 0, 10, user_id="u1") is None
    assert await store.read(rel2, 0, 10, user_id="u1") is not None


@pytest.mark.asyncio
async def test_read_file_tool_reads_and_isolates(tmp_path):
    """read_file 工具：按 path + offset 读片段，按 current_user_id 隔离。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel = await store.write("conv1", "u1", "abcdefghij")
    registry = ToolRegistry()
    register_file_tools(registry, store)
    current_user_id.set("u1")
    seg = await registry.call_raw("read_file", {"path": rel, "offset": 2, "limit": 3})
    assert seg.startswith("cde")
    current_user_id.set("u2")  # 换用户：读不到
    seg2 = await registry.call_raw("read_file", {"path": rel})
    assert seg2 == "未找到该文件或无权访问"


@pytest.mark.asyncio
async def test_grep_tool_searches(tmp_path):
    """grep 工具：按 path + 正则搜，返回命中。"""
    store = ToolResultStore(str(tmp_path / "results"))
    rel = await store.write("conv1", "u1", "AAAMU5160BBB")
    registry = ToolRegistry()
    register_file_tools(registry, store)
    current_user_id.set("u1")
    res = await registry.call_raw("grep", {"pattern": "MU5160", "path": rel})
    assert res["count"] == 1
    assert res["matches"][0]["match"] == "MU5160"


@pytest.mark.asyncio
async def test_run_stream_spills_oversize_result(tmp_path):
    """run_stream：超阈值工具结果写临时文件，窗口放预览 + 路径提示，完整结果保留在文件。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "big", "arguments": "{}"}}]}],
        [{"type": "content", "text": "ok"}, {"type": "end", "tool_calls": None}],
    ])
    reg = FakeRegistry({"big": "x" * 5000})
    store = ToolResultStore(str(tmp_path / "results"))
    a = Agent(name="t", system_prompt="s", tools=["big"], llm=llm)
    await a.run_stream("go", [], reg, on_event=None, conversation_id="conv1", tool_result_store=store)
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    content = tool_msgs[0]["content"]
    assert "read_file" in content  # 提示含 read_file/grep
    m = re.search(r"完整结果存到 (\S+)，", content)
    rel = m.group(1)
    seg = await store.read(rel, 0, 6000, user_id="anonymous")
    assert seg["total"] == 5000
    assert seg["content"] == "x" * 5000


@pytest.mark.asyncio
async def test_run_stream_spills_large_dict(tmp_path):
    """dict 键少但 value 大（str 超阈值）也触发 spill，避免截断无兜底导致模型反复重查死循环。"""
    llm = StreamingLLM([
        [{"type": "end", "tool_calls": [{"id": "1", "function": {"name": "big", "arguments": "{}"}}]}],
        [{"type": "content", "text": "ok"}, {"type": "end", "tool_calls": None}],
    ])
    # 2 个键，但 steps 每条 500 字符 × 20 条，str(dict) 超阈值
    reg = FakeRegistry({"big": {"title": "菜", "steps": [{"step": i, "text": "x" * 500} for i in range(20)]}})
    store = ToolResultStore(str(tmp_path / "results"))
    a = Agent(name="t", system_prompt="s", tools=["big"], llm=llm)
    await a.run_stream("go", [], reg, on_event=None, conversation_id="conv1", tool_result_store=store)
    tool_msgs = [m for m in llm.calls[1] if m["role"] == "tool"]
    content = tool_msgs[0]["content"]
    assert "read_file" in content  # 键少但 str 长，也触发 spill 写文件
