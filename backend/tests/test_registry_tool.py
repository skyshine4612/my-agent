# tests/test_registry_tool.py
# register_tool：从 langchain tool 提取 schema，执行走 tool.ainvoke，拿到的仍是结构化原始结果。
import pytest
from langchain_core.tools import tool

from app.core.registry import ToolRegistry


def test_register_tool_extracts_schema():
    registry = ToolRegistry()

    @tool
    def sample(city: str, days: int = 1) -> dict:
        """查询示例城市的天气。"""
        return {"city": city, "days": days}

    registry.register_tool(sample, "查天气")
    schemas = registry.to_openai_schemas(["sample"])
    assert len(schemas) == 1
    fn = schemas[0]["function"]
    assert fn["name"] == "sample"
    assert fn["description"] == "查询示例城市的天气。"
    assert "city" in fn["parameters"]["properties"]
    assert fn["parameters"]["properties"]["city"]["type"] == "string"


def test_register_tool_label():
    registry = ToolRegistry()

    @tool
    def sample(city: str) -> dict:
        """查询示例城市的天气。"""
        return {"city": city}

    registry.register_tool(sample, "查天气")
    assert registry.label("sample") == "查天气"


@pytest.mark.asyncio
async def test_call_raw_returns_structured_result():
    """call_raw 必须拿到原始 dict —— 这是 spill 判断与结构化截断的前提。"""
    registry = ToolRegistry()

    @tool
    def sample(city: str) -> dict:
        """查询示例城市的天气。"""
        return {"city": city, "items": list(range(30))}

    registry.register_tool(sample, "查天气")
    raw = await registry.call_raw("sample", {"city": "杭州"})
    assert isinstance(raw, dict)
    assert raw["city"] == "杭州"
    assert raw["items"] == list(range(30))


@pytest.mark.asyncio
async def test_register_tool_supports_async():
    """纯异步工具：call_raw 内部走 ainvoke，同步/异步工具都统一处理。"""
    registry = ToolRegistry()

    @tool
    async def sample_async(city: str) -> dict:
        """异步查询示例城市的天气。"""
        return {"city": city}

    registry.register_tool(sample_async, "查天气")
    raw = await registry.call_raw("sample_async", {"city": "北京"})
    assert raw == {"city": "北京"}


@pytest.mark.asyncio
async def test_call_raw_coerces_types():
    """call_raw 走 ainvoke，白拿 pydantic 类型强转：字符串 "3" 交给 int 形参。"""
    registry = ToolRegistry()

    @tool
    def sample(days: int) -> dict:
        """查询示例天数。"""
        return {"days": days, "type": type(days).__name__}

    registry.register_tool(sample, "查天数")
    raw = await registry.call_raw("sample", {"days": "3"})
    assert raw == {"days": 3, "type": "int"}


def test_optional_param_schema_is_flattened():
    """Optional 参数产生 anyOf:[X, null]，压平成 X —— OpenAI 对 anyOf 支持有限。"""
    registry = ToolRegistry()

    @tool
    def sample(city: str, note: str | None = None) -> dict:
        """查询示例城市的天气。"""
        return {"city": city, "note": note}

    registry.register_tool(sample, "查天气")
    props = registry.to_openai_schemas(["sample"])[0]["function"]["parameters"]["properties"]
    assert props["note"]["type"] == "string"
    assert "anyOf" not in props["note"]
