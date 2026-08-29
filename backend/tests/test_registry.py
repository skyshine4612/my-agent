# tests/test_registry.py
# 工具/Agent 注册表的契约测试：验证工具转 OpenAI schema、同步/异步调用统一返回字符串、
# 以及 Agent 能力清单文本的生成。
import pytest

from app.core.registry import ToolRegistry, AgentRegistry
from app.core.agent import Agent


@pytest.mark.asyncio
async def test_tool_registry_schema_and_call():
    r = ToolRegistry()
    r.register("weather", "查天气", {"type": "object", "properties": {"city": {"type": "string"}}},
               lambda city: f"{city}晴")
    schemas = r.to_openai_schemas(["weather"])
    assert schemas[0]["function"]["name"] == "weather"
    assert await r.call("weather", {"city": "成都"}) == "成都晴"


def test_agent_registry_describe():
    ar = AgentRegistry()
    ar.register(Agent(name="天气Agent", system_prompt="查天气", tools=["weather"], llm=None))
    assert "天气Agent" in ar.describe()
    assert ar.get("天气Agent").name == "天气Agent"
