# tests/test_registry.py
# 工具注册表的契约测试：验证工具转 OpenAI schema、同步/异步调用统一返回字符串。
import pytest
from langchain_core.tools import tool

from app.core.registry import ToolRegistry


@pytest.mark.asyncio
async def test_tool_registry_schema_and_call():
    r = ToolRegistry()

    @tool
    def weather(city: str) -> str:
        """查天气。"""
        return f"{city}晴"

    r.register_tool(weather, "查天气")
    schemas = r.to_openai_schemas(["weather"])
    assert schemas[0]["function"]["name"] == "weather"
    assert await r.call("weather", {"city": "成都"}) == "成都晴"
