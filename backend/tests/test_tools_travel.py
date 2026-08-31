# tests/test_tools_travel.py
# 旅行工具工厂的契约测试：用鸭子类型 FakeDS 验证工具工厂的注入与调用。
import pytest

from app.tools.travel import (
    make_poi_search, make_poi_detail,
    make_train_ticket_query, make_flight_query,
)


class FakeDS:
    """高德数据源假实现：返回固定 POI。"""
    async def search_poi(self, keywords, city, **kw):
        return [{"name": "宽窄巷子", "location": {"lng": 1, "lat": 1}, "price": 0}]

    async def get_poi_detail(self, poi_id):
        return {"name": "宽窄巷子", "address": "成都", "tag": "龙井虾仁,脆皮大肠"}


class FakeTrainDS:
    """12306 假数据源：返回固定票务文本。"""
    async def search_tickets(self, date, from_city, to_city):
        return f"{date} {from_city}->{to_city} G101 二等座 553元"


class FakeFlightDS:
    """机票假数据源：返回固定航班文本。"""
    async def search_flights(self, date, from_city, to_city):
        return f"{date} {from_city}->{to_city} CA123 经济舱 1200元"


@pytest.mark.asyncio
async def test_poi_search():
    fn = make_poi_search(FakeDS())
    pois = await fn(keywords="景点", city="成都")
    assert pois[0]["name"] == "宽窄巷子"


@pytest.mark.asyncio
async def test_poi_search_without_city():
    """poi_search 的 city 可选：不传 city 时全局搜索不抛 TypeError。"""
    fn = make_poi_search(FakeDS())
    pois = await fn(keywords="景点")
    assert pois[0]["name"] == "宽窄巷子"


@pytest.mark.asyncio
async def test_poi_detail():
    fn = make_poi_detail(FakeDS())
    r = await fn(id="B001")
    assert r["tag"] == "龙井虾仁,脆皮大肠"


@pytest.mark.asyncio
async def test_train_ticket_query():
    fn = make_train_ticket_query(FakeTrainDS())
    r = await fn(date="2026-08-30", from_city="成都", to_city="北京")
    assert "G101" in r and "553" in r


@pytest.mark.asyncio
async def test_flight_query():
    fn = make_flight_query(FakeFlightDS())
    r = await fn(date="2026-08-30", from_city="成都", to_city="北京")
    assert "CA123" in r and "1200" in r
