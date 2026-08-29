# tests/test_amap_datasource.py
# 数据源契约测试 + 高德工具名映射/结果解析单测。
# 契约测试用 FakeDS 验证 DataSource 接口形状；映射/解析测试只测纯函数，不触碰真实 MCP 连接
# （真实连接依赖 Task 9 冒烟）。
import pytest

from app.datasource.base import DataSource
from app.datasource.amap_mcp import (
    resolve_tool_name,
    route_tool_for_mode,
    parse_poi_list,
    parse_weather,
    parse_geocode,
)


class FakeDS(DataSource):
    """假数据源：实现 DataSource 全部抽象方法，返回固定契约结构，用于验证接口形状。"""
    async def search_poi(self, k, c, **kw): return [{"name": "x", "location": {"lng": 1, "lat": 1}, "price": 0}]
    async def get_weather(self, c, d): return [{"date": "2026-08-29", "day_weather": "晴"}]
    async def plan_route(self, o, de, m): return {"mode": m}
    async def geocode(self, a): return {"lng": 1, "lat": 1}


@pytest.mark.asyncio
async def test_datasource_contract():
    """契约测试：任何实现 DataSource 的类都应能按约定形状调用 search_poi。"""
    ds = FakeDS()
    assert (await ds.search_poi("景点", "成都"))[0]["name"] == "x"


def test_amap_tool_name_mapping():
    """工具名映射：契约方法 → 高德 MCP 工具名。"""
    assert resolve_tool_name("search_poi") == "maps_text_search"
    assert resolve_tool_name("get_weather") == "maps_weather"
    assert resolve_tool_name("geocode") == "maps_geo"


def test_amap_route_tool_by_mode():
    """路径规划按出行方式映射，未识别方式兜底驾车。"""
    assert resolve_tool_name("plan_route", "walking") == "maps_direction_walking"
    assert resolve_tool_name("plan_route", "driving") == "maps_direction_driving"
    assert resolve_tool_name("plan_route", "transit") == "maps_direction_transit_integrated"
    assert route_tool_for_mode("flying") == "maps_direction_driving"


def test_parse_poi_list_contract():
    """POI 解析：把高德 text_search 返回解析成契约结构，location 字符串转 {lng,lat}。"""
    payload = {"pois": [{
        "id": "B001", "name": "宽窄巷子", "address": "成都市青羊区", "location": "104.06,30.68",
        "typecode": "风景名胜;街区", "biz_ext": {"cost": "0"},
    }]}
    pois = parse_poi_list(payload)
    assert pois[0]["name"] == "宽窄巷子"
    assert pois[0]["location"] == {"lng": 104.06, "lat": 30.68}
    assert pois[0]["category"] == "风景名胜;街区"   # 高德用 typecode 字段表示分类
    assert pois[0]["price"] == 0.0


def test_parse_weather_contract():
    """天气解析：把 maps_weather 的 forecasts/casts 解析成 date/day_weather/day_temp/night_temp 列表。"""
    payload = {"forecasts": [{"casts": [{"date": "2026-08-29", "dayweather": "晴", "daytemp": "31", "nighttemp": "22"}]}]}
    assert parse_weather(payload) == [{"date": "2026-08-29", "day_weather": "晴", "day_temp": 31, "night_temp": 22}]


def test_parse_weather_missing_temp_defaults_to_zero():
    """天气解析：温度字段缺失时兜底为 0，保证前端不会渲染 undefined。"""
    payload = {"forecasts": [{"casts": [{"date": "2026-08-29", "dayweather": "晴"}]}]}
    assert parse_weather(payload) == [{"date": "2026-08-29", "day_weather": "晴", "day_temp": 0, "night_temp": 0}]


def test_parse_geocode_contract():
    """地理编码解析：geocodes[0].location 转 {lng,lat}。"""
    payload = {"geocodes": [{"location": "116.397,39.908"}]}
    assert parse_geocode(payload) == {"lng": 116.397, "lat": 39.908}
