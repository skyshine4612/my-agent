# tests/test_sub_agent.py
# call_sub_agent 工具的契约测试：验证委派子任务返回摘要。
import pytest

from app.core.memory import ShortTermMemory
from app.core.registry import ToolRegistry
from app.tools.sub_agent import make_call_sub_agent


class FakeSubLLM:
    """子 Agent 的假 LLM：直接吐「子任务完成」，不调工具。"""

    async def stream_chat(self, messages, tools=None):
        yield {"type": "content", "text": "子任务完成"}
        yield {"type": "end", "tool_calls": None}

    async def chat(self, messages, tools=None, response_format=None):
        return {"content": "{}", "tool_calls": None}

    async def complete(self, messages):
        return ""


@pytest.mark.asyncio
async def test_call_sub_agent_returns_answer(tmp_path):
    registry = ToolRegistry()

    async def get_skill(name):
        return f"{name} 规则"

    get_skill.__name__ = "get_skill"
    registry.register("get_skill", "读取业务规则", {"type": "object", "properties": {}}, get_skill, "读取业务规则")

    stm = ShortTermMemory(str(tmp_path / "st.db"))
    fn = make_call_sub_agent(FakeSubLLM(), registry, ["travel"], stm, 32000)
    result = await fn(task="查北京到上海的火车票")
    assert result["answer"] == "子任务完成"
