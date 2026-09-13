# tests/test_sub_agent.py
# call_sub_agent 工具的契约测试：验证委派子任务返回摘要。
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.core.context import current_user_id
from app.core.memory import ToolResultStore
from app.core.registry import ToolRegistry
from app.tools.sub_agent import make_call_sub_agent


class SubChatModel(BaseChatModel):
    """子 Agent 的假 langchain 模型（子 Agent 的循环由 create_agent 驱动）：直接回答案、不调工具。"""

    @property
    def _llm_type(self):
        return "sub-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="子任务完成"))])


class TwoStepChatModel(BaseChatModel):
    """两步假模型：第一步回一次工具调用（让子 Agent 真的落一次 spill），之后回答案。"""

    step: int = 0

    @property
    def _llm_type(self):
        return "sub-two-step"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.step += 1
        if self.step == 1:
            msg = AIMessage(content="", tool_calls=[
                {"name": "list_items", "args": {"city": "北京"}, "id": "c1"}])
        else:
            msg = AIMessage(content="子任务完成")
        return ChatResult(generations=[ChatGeneration(message=msg)])


@pytest.mark.asyncio
async def test_call_sub_agent_returns_answer(tmp_path):
    registry = ToolRegistry()

    @tool
    async def get_skill(name: str) -> str:
        """读取业务规则。"""
        return f"{name} 规则"

    registry.register_tool(get_skill, "读取业务规则")

    fn = make_call_sub_agent(SubChatModel(), registry, ["travel"], 32000)
    result = await fn.ainvoke({"task": "查北京到上海的火车票"})
    assert result["answer"] == "子任务完成"


@pytest.mark.asyncio
async def test_call_sub_agent_cleans_up_sub_conversation(tmp_path):
    """传入 tool_result_store 时，子任务结束要清掉子会话目录（子 Agent 落过的 spill 一并带走）。

    传 store 才是生产形态（service 装配时恒传），不传时 finally 里那段清理不会执行。
    """
    registry = ToolRegistry()

    @tool
    async def list_items(city: str) -> list:
        """查列表。"""
        return [f"{city}-{i}" for i in range(30)]   # 超 list_limit=20，触发地址索引落盘

    registry.register_tool(list_items, "查列表")

    store = ToolResultStore(str(tmp_path))
    token = current_user_id.set("u1")
    try:
        fn = make_call_sub_agent(TwoStepChatModel(), registry, ["travel"], 32000, store)
        result = await fn.ainvoke({"task": "查北京有什么"})
    finally:
        current_user_id.reset(token)

    assert result["answer"] == "子任务完成"
    # 子会话目录连同 spill 文件已清掉；用户目录随之空了也一并删
    assert not (tmp_path / "u1").exists()
