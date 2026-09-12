# tests/test_sub_agent.py
# call_sub_agent 工具的契约测试：验证委派子任务返回摘要。
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

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


class SubChatModel(BaseChatModel):
    """子 Agent 的假 langchain 模型（子 Agent 的循环由 create_agent 驱动）：直接回答案、不调工具。"""

    @property
    def _llm_type(self):
        return "sub-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="子任务完成"))])


@pytest.mark.asyncio
async def test_call_sub_agent_returns_answer(tmp_path):
    registry = ToolRegistry()

    @tool
    async def get_skill(name: str) -> str:
        """读取业务规则。"""
        return f"{name} 规则"

    registry.register_tool(get_skill, "读取业务规则")

    fn = make_call_sub_agent(FakeSubLLM(), registry, ["travel"], 32000, model=SubChatModel())
    result = await fn.ainvoke({"task": "查北京到上海的火车票"})
    assert result["answer"] == "子任务完成"
