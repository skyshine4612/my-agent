# tests/test_registry.py
# 工具注册表的契约测试：验证工具转 OpenAI schema、同步/异步调用统一返回字符串。
import pytest

from app.core.registry import ToolRegistry


@pytest.mark.asyncio
async def test_tool_registry_schema_and_call():
    r = ToolRegistry()
    r.register("weather", "查天气", {"type": "object", "properties": {"city": {"type": "string"}}},
               lambda city: f"{city}晴")
    schemas = r.to_openai_schemas(["weather"])
    assert schemas[0]["function"]["name"] == "weather"
    assert await r.call("weather", {"city": "成都"}) == "成都晴"
