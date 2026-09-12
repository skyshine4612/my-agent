# app/tools/travel.py
# 旅行工具工厂 + 注册：POI / 火车票 / 机票 / 模糊搜索。
# 工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
# 参数描述用 Annotated[T, Field(description=...)]：它是模型可见契约，裸注解会静默丢掉。
# 注意：train_ticket_query / flight_query 的 date / from_city / to_city 六个参数不写描述。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.datasource.flight_variflight import FlightVariflightDataSource
from app.datasource.train_12306 import Train12306DataSource


def make_poi_search(ds):
    """POI 搜索工具：按关键词或分类码搜索景点/酒店/美食，可限定城市。"""

    @tool
    async def poi_search(
        keywords: Annotated[str, Field(description="搜索关键词（地点名，如 西湖、灵隐寺）")] = "",
        city: Annotated[str, Field(description="限定城市，可选，不填则全局搜索")] = "",
        types: Annotated[str, Field(description="高德分类码（110000风景名胜/140000科教文化/050000餐饮/100000住宿）")] = "",
        sortrule: Annotated[str, Field(description="排序规则：distance(按距离)/weight(综合推荐)/rating(按评分)，不填默认综合")] = "",
    ) -> list[dict]:
        """按关键词或分类码搜索POI(景点/酒店/美食)，可限定城市，返回名称/地址等基本信息"""
        pois = await ds.search_poi(keywords=keywords, city=city, types=types, sortrule=sortrule)
        return pois or [
            {"name": "未找到结果", "address": "", "location": None, "category": keywords or types, "price": 0}]

    return poi_search


def make_poi_detail(ds):
    """POI 详情工具：按 id 查地址与推荐菜。"""

    @tool
    async def poi_detail(id: Annotated[str, Field(description="POI id，来自 poi_search 返回")]) -> dict:
        """调用前必须先调用 poi_search，按 poi_search 返回的 POI id 查询详情，返回名称/地址/推荐菜标签（餐饮类 POI 的 tag 含招牌菜）"""
        return await ds.get_poi_detail(id)

    return poi_detail


def make_train_ticket_query(train_ds):
    """火车票查询工具：查出发地到目的地的火车票（车次/时间/票价/余票）。"""

    @tool
    async def train_ticket_query(date: str, from_city: str, to_city: str) -> str:
        """查询跨城火车票(车次/时间/票价/余票)"""
        return await train_ds.search_tickets(date, from_city, to_city)

    return train_ticket_query


def make_flight_query(flight_ds):
    """机票查询工具：查出发地到目的地的机票（航班/时间/票价）。"""

    @tool
    async def flight_query(date: str, from_city: str, to_city: str) -> str:
        """查询跨城机票(航班/时间/票价)"""
        return await flight_ds.search_flights(date, from_city, to_city)

    return flight_query


def register_travel_tools(registry, amap_web_ds):
    """把旅行工具注入 ToolRegistry（高德 Web API 工具共用传入的数据源实例）。"""
    registry.register_tool(make_poi_search(amap_web_ds), "搜索景点/酒店/美食")
    registry.register_tool(make_poi_detail(amap_web_ds), "搜索POI详情")
    # 火车/机票 MCP 数据源内联 new（其 __init__ 会创建从不 close 的连接，只在启动时 new 一次）
    train_ds = Train12306DataSource()
    flight_ds = FlightVariflightDataSource()
    registry.register_tool(make_train_ticket_query(train_ds), "查询火车票")
    registry.register_tool(make_flight_query(flight_ds), "查询机票")
    # 返回本函数内联 new 的 MCP 数据源（供上层统一 close）
    return [train_ds, flight_ds]
