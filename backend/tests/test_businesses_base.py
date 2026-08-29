# tests/test_businesses_base.py
# Business 抽象契约 + TravelBusiness 元信息契约测试：
# 验证 Business 是抽象类、TravelBusiness 的 name/description/tool_names 与 build_sub_agent 装配正确。
import pytest

from app.businesses.base import Business
from app.businesses.travel import TravelBusiness


def test_business_is_abstract():
    """Business 声明抽象方法 register_tools，无法直接实例化。"""
    with pytest.raises(TypeError):
        Business()


def test_travel_business_metadata():
    """TravelBusiness 的 name/description/tool_names 与接口约定一致，rules 从 prompts/travel.md 加载且非空。"""
    b = TravelBusiness()
    assert b.name == "travel"
    assert b.description == "旅行规划（行程/交通/景点/天气/预算）"
    assert b.tool_names == ["poi_search", "weather_query", "route_plan", "train_ticket_query", "flight_query"]
    assert b.rules and "旅行规划专家" in b.rules


def test_travel_business_build_sub_agent():
    """build_sub_agent 用基类实现：system prompt = rules，tools = tool_names + ["call_sub_agent"]。"""
    b = TravelBusiness()
    agent = b.build_sub_agent(llm=None)
    assert agent.name == "travel"
    assert agent.system_prompt == b.rules
    assert agent.tools == b.tool_names + ["call_sub_agent"]
