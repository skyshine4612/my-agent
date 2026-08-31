# app/tools/travel.py
# 旅行工具工厂 + 注册：POI / 火车票 / 机票 / 模糊搜索。
from app.datasource.flight_variflight import FlightVariflightDataSource
from app.datasource.train_12306 import Train12306DataSource


def make_poi_search(ds):
    """POI 搜索工具：按关键词搜索景点/酒店/美食，可限定城市。"""
    async def poi_search(keywords, city=None):
        """按关键词搜索 POI（景点/酒店/美食），city 可选（不填则全局搜索）；无结果时返回占位条目。"""
        pois = await ds.search_poi(keywords, city)
        return pois or [{"name": "未找到结果", "address": "", "location": None, "category": keywords, "price": 0}]
    return poi_search


def make_train_ticket_query(train_ds):
    """火车票查询工具：查出发地到目的地的火车票（车次/时间/票价/余票）。"""
    async def train_ticket_query(date, from_city, to_city):
        """按日期查询出发地到目的地的火车票（车次/时间/票价/余票）。"""
        return await train_ds.search_tickets(date, from_city, to_city)
    return train_ticket_query


def make_flight_query(flight_ds):
    """机票查询工具：查出发地到目的地的机票（航班/时间/票价）。"""
    async def flight_query(date, from_city, to_city):
        """按日期查询出发地到目的地的机票（航班/时间/票价）。"""
        return await flight_ds.search_flights(date, from_city, to_city)
    return flight_query


def register_travel_tools(registry, amap_web_ds):
    """把旅行工具注入 ToolRegistry（高德 Web API 工具共用传入的数据源实例）。"""
    registry.register("poi_search", "按关键词搜索POI(景点/酒店/美食)，可限定城市，返回名称/地址等基本信息",
        {"type": "object", "properties": {"keywords": {"type": "string", "description": "搜索关键词"}, "city": {"type": "string", "description": "限定城市，可选，不填则全局搜索"}}, "required": ["keywords"]},
        make_poi_search(amap_web_ds), "搜索景点/酒店")
    # 火车/机票 MCP 数据源内联 new（其 __init__ 会创建从不 close 的连接，只在启动时 new 一次）
    registry.register("train_ticket_query", "查询跨城火车票(车次/时间/票价/余票)",
        {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
        make_train_ticket_query(Train12306DataSource()), "查询火车票")
    registry.register("flight_query", "查询跨城机票(航班/时间/票价)",
        {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
        make_flight_query(FlightVariflightDataSource()), "查询机票")
