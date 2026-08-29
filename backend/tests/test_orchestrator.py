# tests/test_orchestrator.py
# 编排器与规划器契约测试：拓扑分层、层内并行执行、结果卸载，以及 Planner 的澄清/tasks 分支。
# 全部使用 EchoAgent / FakeLLM 等假实现，不触碰真实 LLM 或外部 API。
import asyncio

import pytest

from app.core.orchestrator import Orchestrator, SubTask, topo_layers
from app.core.planner import Planner


def test_topo_layers():
    """拓扑分层：T3 依赖 T1、T2，故第一层只有 T1、T2，可并行。"""
    ts = [SubTask("T1", "a", {}, []), SubTask("T2", "b", {}, []), SubTask("T3", "c", {}, ["T1", "T2"])]
    layers = topo_layers(ts)
    assert {t.id for t in layers[0]} == {"T1", "T2"}


class EchoAgent:
    """回显假 Agent：返回 "<"+消息+">"，用于验证并行调度与结果传递，不依赖真实 LLM。"""
    async def run(self, msg, history, reg, on_step=None): await asyncio.sleep(0.01); return "<" + str(msg) + ">"


class AR:
    """假 Agent 注册表：按名返回 EchoAgent。"""
    def get(self, n): return EchoAgent()


class TM:
    """假任务记忆：只满足 Orchestrator 所需接口，不落库。"""
    async def create_task(self, c, p): return "tid"
    async def save_subtask_result(self, *a): pass


@pytest.mark.asyncio
async def test_execute_parallel():
    """层内并行执行：T1、T2 无依赖先并行跑，T3 依赖二者后跑；结果按 id 收集。"""
    o = Orchestrator(AR(), TM())
    ts = [SubTask("T1", "e", {"x": "1"}, []), SubTask("T2", "e", {"x": "2"}, []), SubTask("T3", "e", {"x": "3"}, ["T1", "T2"])]
    res = await o.execute(ts, [], None)
    assert res["T3"] and res["T1"]


class FakeLLM:
    """假 LLM：chat 固定返回预设 content，供 Planner 测试，不触碰真实 API。"""
    def __init__(self, content): self.content = content
    async def chat(self, messages, tools=None, response_format=None): return {"content": self.content}


class FakeRegistry:
    """假 Agent 注册表：只提供 describe() 能力清单文本。"""
    def describe(self): return "- 天气Agent: 查天气"


@pytest.mark.asyncio
async def test_plan_clarify():
    """澄清分支：LLM 返回 clarify 时，plan() 原样返回该 dict（含缺失项问题清单）。"""
    p = Planner(FakeLLM('{"clarify":true,"questions":["去哪里"]}'), FakeRegistry(), ["目的地", "出行天数", "预算"])
    r = await p.plan("帮我安排一次旅行")
    assert r == {"clarify": True, "questions": ["去哪里"]}


@pytest.mark.asyncio
async def test_plan_tasks():
    """拆解分支：LLM 返回 tasks 时，plan() 返回含 tasks 的 dict。"""
    p = Planner(FakeLLM('{"tasks":[{"id":"T1","agent":"天气Agent","params":{},"depends_on":[]}]}'),
                FakeRegistry(), ["目的地", "出行天数", "预算"])
    r = await p.plan("去成都玩4天预算5000")
    assert "tasks" in r and r["tasks"][0]["id"] == "T1"
