# app/businesses/travel/__init__.py
# 旅行业务：声明业务元信息（name/description/rules/tool_names），
# 并把旅行工具（amap 3 个 + train/flight 2 个）注入 ToolRegistry。
from app.businesses.base import Business
from app.businesses.travel import tools
from app.core.prompts import load_prompt
from app.datasource.flight_variflight import FlightVariflightDataSource
from app.datasource.train_12306 import Train12306DataSource


class TravelBusiness(Business):
    """旅行规划业务：行程/交通/景点/天气/预算。"""
    name = "travel"
    description = "旅行规划（行程/交通/景点/天气/预算）"
    # 业务规则从 prompts/travel.md 加载，作为子 Agent 的 system prompt 主体
    rules = load_prompt("travel")
    tool_names = ["poi_search", "weather_query", "route_plan", "train_ticket_query", "flight_query"]

    def register_tools(self, registry, deps):
        """把旅行业务 5 个工具注入 ToolRegistry。

        参数：
            registry: ToolRegistry，工具注册表
            deps:     AmapMcpDataSource（沿用 domains 旧代码把数据源直接传入的注册方式）
        """
        amap = deps
        # 三个高德工具：共用同一个 amap 数据源（search_poi / get_weather / plan_route）
        registry.register("poi_search", "按关键词搜索POI(景点/酒店/美食)",
            {"type": "object", "properties": {"keywords": {"type": "string"}, "city": {"type": "string"}, "price_max": {"type": "number"}}, "required": ["keywords", "city"]},
            tools.make_poi_search(amap))
        registry.register("weather_query", "查询城市未来天气",
            {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}, "required": ["city"]},
            tools.make_weather_query(amap))
        registry.register("route_plan", "规划两点间路线",
            {"type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "mode": {"type": "string"}}, "required": ["origin", "destination"]},
            tools.make_route_plan(amap))
        # 火车/机票数据源内联 new（与 domains/travel/agents.py 一致）
        registry.register("train_ticket_query", "查询跨城火车票(车次/时间/票价/余票)",
            {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
            tools.make_train_ticket_query(Train12306DataSource()))
        registry.register("flight_query", "查询跨城机票(航班/时间/票价)",
            {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
            tools.make_flight_query(FlightVariflightDataSource()))
