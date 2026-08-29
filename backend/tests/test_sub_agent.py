# tests/test_sub_agent.py
# call_sub_agent 工具契约测试：
# 验证正常委派返回子 Agent 的 answer、contextvar 深度上限（第 3 层嵌套被拒绝）、enum 动态取自业务名。
import asyncio
import json

import pytest

from app.core.agent import Agent
from app.core.sub_agent import make_call_sub_agent, _sub_call_depth, _MAX_SUB_CALL_DEPTH


class AnswerLLM:
    """固定返回文本答案、不调工具的 LLM 假实现（实现 stream_chat 异步生成器）。"""
    def __init__(self, text="已规划完成"):
        self.text = text

    async def stream_chat(self, messages, tools=None):
        yield {"type": "content", "text": self.text}
        yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": self.text, "tool_calls": None}

    async def complete(self, messages):
        return ""


class NestLLM:
    """按调用次数分阶段产出的 LLM 假实现：前两次各发起一次 call_sub_agent（制造父子两层），之后直接给答案。"""
    def __init__(self):
        self.n = 0

    async def stream_chat(self, messages, tools=None):
        self.n += 1
        if self.n <= 2:
            # 第 1、2 次：发起 call_sub_agent，把嵌套继续往下推一层
            yield {"type": "end", "tool_calls": [{
                "id": str(self.n),
                "function": {"name": "call_sub_agent",
                             "arguments": json.dumps({"subagent_type": "travel", "task": f"第{self.n}层"})},
            }]}
        else:
            # 第 3 次起：直接给答案，结束当前层
            yield {"type": "content", "text": "层答案"}
            yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "层答案", "tool_calls": None}

    async def complete(self, messages):
        return ""


class FakeBusiness:
    """业务假实现：build_sub_agent 返回不带额外工具的 Agent（复用传入 llm）。"""
    def __init__(self, name="travel", rules="旅行规则"):
        self.name = name
        self.rules = rules

    def build_sub_agent(self, llm):
        return Agent(name=self.name, system_prompt=self.rules, tools=[], llm=llm)


class SpyRegistry:
    """工具注册表假实现：call 记录每次调用与字符串结果，to_openai_schemas 返回空。"""
    def __init__(self):
        self.tools = {}
        self.calls = []
        self.results = []

    async def call(self, name, args):
        self.calls.append((name, args))
        fn = self.tools[name]
        res = fn(**args)
        if asyncio.iscoroutine(res):
            res = await res
        out = str(res)
        self.results.append(out)
        return out

    def to_openai_schemas(self, names):
        return []


@pytest.mark.asyncio
async def test_call_sub_agent_delegates_and_returns_answer():
    """call_sub_agent 按 subagent_type 找到业务，构建子 Agent 并返回其最终文本作为 answer。"""
    reg = SpyRegistry()
    llm = AnswerLLM("成都三日游方案")
    tool = make_call_sub_agent({"travel": FakeBusiness()}, llm, reg)
    result = await tool("travel", "规划成都三日游")
    assert result == {"answer": "成都三日游方案"}


@pytest.mark.asyncio
async def test_call_sub_agent_rejects_when_depth_exceeded():
    """深度已达上限（即将进入第 3 层）时直接拒绝，不再构建子 Agent。"""
    reg = SpyRegistry()
    tool = make_call_sub_agent({"travel": FakeBusiness()}, AnswerLLM("不应被调用"), reg)
    token = _sub_call_depth.set(_MAX_SUB_CALL_DEPTH)
    try:
        result = await tool("travel", "任务")
    finally:
        _sub_call_depth.reset(token)
    assert result == {"answer": "子 Agent 嵌套已达上限"}
    assert reg.calls == []  # 被拒绝时不触发任何子 Agent 工具调用


@pytest.mark.asyncio
async def test_call_sub_agent_rejects_third_level():
    """嵌套到第 3 层时被深度上限拒绝，递归正常收敛，最终返回最深一层的答案。"""
    reg = SpyRegistry()
    business = FakeBusiness()
    tool = make_call_sub_agent({"travel": business}, NestLLM(), reg)
    # 把工具挂进 registry，供子 Agent 的 run_stream 真正调用到 call_sub_agent
    reg.tools["call_sub_agent"] = tool
    result = await tool("travel", "顶层任务")
    # 顶层最终拿到最深一层的答案（递归正常收敛，未被无限下钻）
    assert result["answer"] == "层答案"
    # 共发生两次 call_sub_agent：按深度优先，最内层（第 3 层）先执行并触发深度上限拒绝
    assert [name for name, _ in reg.calls] == ["call_sub_agent", "call_sub_agent"]
    assert "子 Agent 嵌套已达上限" in reg.results[0]


def test_call_sub_agent_enum_reflects_businesses():
    """subagent_type 的 JSON schema enum 动态取自 businesses 的 keys。"""
    reg = SpyRegistry()
    tool = make_call_sub_agent({"travel": FakeBusiness(), "food": FakeBusiness("food")}, AnswerLLM(), reg)
    assert tool.parameters["properties"]["subagent_type"]["enum"] == ["travel", "food"]
