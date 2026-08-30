# app/tools/travel.py
# 旅行工具工厂 + 注册：POI / 天气 / 火车票 / 机票，注入对应数据源后返回 async 工具函数。
from app.datasource.flight_variflight import FlightVariflightDataSource
from app.datasource.train_12306 import Train12306DataSource


def make_poi_search(ds):
    """POI 搜索工具：按关键词+城市搜索景点/酒店/美食。"""
    async def poi_search(keywords, city, price_max=None):
        """按关键词+城市搜索 POI（景点/酒店/美食），可选价格上限过滤；无结果时返回占位条目。"""
        kw = {"price_max": price_max} if price_max else {}
        pois = await ds.search_poi(keywords, city, **kw)
        return pois or [{"name": "未找到结果", "address": "", "location": None, "category": keywords, "price": 0}]
    return poi_search


def make_weather_query(ds):
    """天气查询工具：查城市未来 N 天天气。"""
    async def weather_query(city, days=3):
        """查询指定城市未来 N 天（默认 3 天）的天气，返回天气列表。"""
        return await ds.get_weather(city, days)
    return weather_query


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


def register_travel_tools(registry, amap_ds):
    """把旅行 4 个工具注入 ToolRegistry。

    参数：
        registry: ToolRegistry，工具注册表
        amap_ds:  高德数据源（poi_search / weather_query 共用）
    """
    registry.register("poi_search", "按关键词搜索POI(景点/酒店/美食)，仅返回名称/地址，不含门票价/开放时间/实时房价",
        {"type": "object", "properties": {"keywords": {"type": "string"}, "city": {"type": "string"}, "price_max": {"type": "number"}}, "required": ["keywords", "city"]},
        make_poi_search(amap_ds))
    registry.register("weather_query", "查询城市未来天气",
        {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}, "required": ["city"]},
        make_weather_query(amap_ds))
    # 火车/机票数据源内联 new（其 __init__ 会创建从不 close 的 httpx 客户端，只在启动时 new 一次）
    registry.register("train_ticket_query", "查询跨城火车票(车次/时间/票价/余票)",
        {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
        make_train_ticket_query(Train12306DataSource()))
    registry.register("flight_query", "查询跨城机票(航班/时间/票价)",
        {"type": "object", "properties": {"date": {"type": "string"}, "from_city": {"type": "string"}, "to_city": {"type": "string"}}, "required": ["date", "from_city", "to_city"]},
        make_flight_query(FlightVariflightDataSource()))
