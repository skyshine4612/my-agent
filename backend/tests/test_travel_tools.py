import pytest
from app.datasource.base import DataSource
from app.domains.travel.tools import make_poi_search, make_weather_query, make_budget_calc

class FakeDS(DataSource):
    async def search_poi(self, k, c, **kw): return [{"name":"宽窄巷子","location":{"lng":1,"lat":1},"price":0}]
    async def get_weather(self, c, d): return []
    async def plan_route(self, o, de, m): return {}
    async def geocode(self, a): return {}

@pytest.mark.asyncio
async def test_poi_search():
    fn = make_poi_search(FakeDS())
    pois = await fn(keywords="景点", city="成都")
    assert pois[0]["name"] == "宽窄巷子"

def test_budget_calc():
    r = make_budget_calc()(items=[{"name":"酒店","cost":280}], total_budget=3000)
    assert r["total"] == 280 and r["within_budget"] is True

@pytest.mark.asyncio
async def test_weather_query_default_days():
    """weather_query 的 days 有默认值：只传 city 不传 days 时不应抛 TypeError。"""
    fn = make_weather_query(FakeDS())
    res = await fn(city="成都")
    assert res == []
