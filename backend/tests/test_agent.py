# tests/test_agent.py
# Agent ReAct 循环与 WorkingMemory 摘要压缩的契约测试：
# 用 ScriptedLLM 预设 tool_calls 序列、FakeRegistry 模拟工具执行，不依赖真实 API。
import pytest

from app.core.agent import Agent, WorkingMemory


class ScriptedLLM:
    """按脚本顺序返回预设回复的 LLM 假实现：chat 依序吐 turns，complete 固定返回摘要。"""
    def __init__(self, turns):
        self.turns = list(turns)
        self.i = 0

    async def chat(self, messages, tools=None, response_format=None):
        r = self.turns[self.i]
        self.i += 1
        return r

    async def complete(self, messages):
        return "摘要"


class FakeRegistry:
    """工具注册表的假实现：call 记录调用并返回预设结果，to_openai_schemas 返回空 schema。"""
    def __init__(self, r):
        self.r = r
        self.called = []

    async def call(self, name, args):
        self.called.append((name, args))
        return self.r[name]

    def to_openai_schemas(self, names):
        return []


@pytest.mark.asyncio
async def test_react_loop():
    """验证 ReAct 循环：模型先发起一次工具调用，工具执行后模型给出最终答案。"""
    llm = ScriptedLLM([
        {"content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "w", "arguments": "{\"c\":\"成都\"}"}}]},
        {"content": "成都晴", "tool_calls": None}])
    reg = FakeRegistry({"w": "晴"})
    a = Agent(name="t", system_prompt="s", tools=["w"], llm=llm)
    out = await a.run("查天气", [], reg)
    assert out == "成都晴" and reg.called == [("w", {"c": "成都"})]


@pytest.mark.asyncio
async def test_working_memory_compression():
    """验证摘要压缩：超长消息列表被蒸馏成少量摘要消息（保留在 system 摘要里），而非简单截断。"""
    llm = ScriptedLLM([])
    wm = WorkingMemory(llm, max_tokens=50)
    messages = [{"role": "user", "content": "x" * 100} for _ in range(30)]
    out = await wm.fit(messages)
    assert len(out) < len(messages)
    assert out[0]["role"] == "system" and "[早期对话摘要]" in out[0]["content"]
