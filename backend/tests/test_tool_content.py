# tests/test_tool_content.py
# 结构化工具的 content/artifact 契约：模型看 content，程序拿 artifact。
import json

import pytest
from langchain_core.messages import ToolMessage

from app.tools.common import make_weather_query


class FakeDS:
    async def get_weather(self, city, days):
        return [{"date": "2026-10-20", "day_weather": "晴"}]


@pytest.mark.asyncio
async def test_structured_tool_exposes_content_and_artifact():
    """结构化工具经 ToolCall 调用时，必须同时给出 content 与原始 artifact。"""
    tool = make_weather_query(FakeDS())
    msg = await tool.ainvoke({
        "type": "tool_call", "id": "tc1", "name": "weather_query",
        "args": {"city": "杭州"},
    })
    assert isinstance(msg, ToolMessage)
    assert msg.artifact == [{"date": "2026-10-20", "day_weather": "晴"}]
    assert json.loads(msg.content) == msg.artifact
