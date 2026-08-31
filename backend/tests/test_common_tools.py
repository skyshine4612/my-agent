# tests/test_common_tools.py
# 通用工具工厂（节假日 + 天气）的契约测试：用 FakeDS 验证注入与调用（不触碰真实 HTTP）。
import pytest

from app.tools.common import (
    make_holiday_calendar, make_weather_query, make_translate, make_hotboard,
    make_recipe_search, make_recipe_detail, make_ingredient_nutrition,
)


class FakeHolidayDS:
    """节假日假数据源：返回固定节假日/农历数据。"""

    async def get_holiday_calendar(self, date="", month="", year="", **kw):
        return {
            "summary": {"total_days": 1},
            "days": [{"date": date, "is_rest_day": True}],
            "holidays": [{"date": date, "name": "国庆节"}],
        }


class FakeWeatherDS:
    """天气假数据源：返回固定天气。"""

    async def get_weather(self, city, days):
        return [{"date": "2026-08-30", "day_weather": "晴"}]


@pytest.mark.asyncio
async def test_holiday_calendar():
    fn = make_holiday_calendar(FakeHolidayDS())
    r = await fn(date="2026-10-01")
    assert r["holidays"][0]["name"] == "国庆节"


@pytest.mark.asyncio
async def test_holiday_calendar_requires_date_month_or_year():
    """holiday_calendar 的 date/month/year 三选一：都不传时返回错误提示而非抛异常。"""
    fn = make_holiday_calendar(FakeHolidayDS())
    r = await fn()
    assert "error" in r


@pytest.mark.asyncio
async def test_weather_query_default_days():
    """weather_query 的 days 有默认值：只传 city 不传 days 时不应抛 TypeError。"""
    fn = make_weather_query(FakeWeatherDS())
    res = await fn(city="成都")
    assert res[0]["day_weather"] == "晴"


class FakeUapisDS:
    """UAPIS 假数据源：返回固定翻译与热榜。"""

    async def translate(self, text, to_lang="en"):
        return {"text": text, "translate": "HELLO"}

    async def get_hotboard(self, type, limit=20):
        return {"type": type, "list": [{"index": 1, "title": "热搜1"}]}


@pytest.mark.asyncio
async def test_translate():
    fn = make_translate(FakeUapisDS())
    r = await fn(text="你好", to_lang="en")
    assert r["translate"] == "HELLO"


@pytest.mark.asyncio
async def test_hotboard():
    fn = make_hotboard(FakeUapisDS())
    r = await fn(type="weibo")
    assert r["list"][0]["title"] == "热搜1"


class FakeRecipeDS:
    """菜谱假数据源：返回固定菜谱与营养。"""

    async def search_recipe(self, keyword):
        return {"keyword": keyword, "items": [{"id": "333", "title": "宫保鸡丁"}]}

    async def get_recipe_detail(self, recipe_id):
        return {"title": "宫保鸡丁", "ingredients": ["鸡腿 2个"], "steps": [{"step": 1, "text": "切丁腌制"}]}

    async def get_ingredient_nutrition(self, keyword):
        return {"name": keyword, "calory": "118", "protein": "24.6"}


@pytest.mark.asyncio
async def test_recipe_search():
    fn = make_recipe_search(FakeRecipeDS())
    r = await fn(keyword="宫保鸡丁")
    assert r["items"][0]["title"] == "宫保鸡丁"


@pytest.mark.asyncio
async def test_recipe_detail():
    fn = make_recipe_detail(FakeRecipeDS())
    r = await fn(id="333")
    assert r["ingredients"][0] == "鸡腿 2个"


@pytest.mark.asyncio
async def test_ingredient_nutrition():
    fn = make_ingredient_nutrition(FakeRecipeDS())
    r = await fn(keyword="鸡胸肉")
    assert r["calory"] == "118"
