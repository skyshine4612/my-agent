# tests/test_common_tools.py
# 通用工具工厂的契约测试：假数据源注入 + 工厂原样透传。
# 六个工厂的形状一致（工厂 → ainvoke(dict) → 取数据源返回值的某个字段），差异只在数据源方法名
# 与取值字段，故假件用一个类收着；每个用例只留自己那条断言，不复制同一份样板的其余部分。
import pytest

from app.tools.common import (
    make_holiday_calendar, make_weather_query, make_translate, make_hotboard,
    make_recipe_search, make_recipe_detail, make_ingredient_nutrition,
)


class FakeDS:
    """通用工具的合体假数据源：每个方法返回固定结构，字段名即真实数据源的契约。"""

    async def get_holiday_calendar(self, date="", month="", year="", **kw):
        return {
            "summary": {"total_days": 1},
            "days": [{"date": date, "is_rest_day": True}],
            "holidays": [{"date": date, "name": "国庆节"}],
        }

    async def get_weather(self, city, days):
        return [{"date": "2026-08-30", "day_weather": "晴"}]

    async def translate(self, text, to_lang="en"):
        return {"text": text, "translate": "HELLO"}

    async def get_hotboard(self, type, limit=20):
        return {"type": type, "list": [{"index": 1, "title": "热搜1"}]}

    async def search_recipe(self, keyword):
        return {"keyword": keyword, "items": [{"id": "333", "title": "宫保鸡丁"}]}

    async def get_recipe_detail(self, recipe_id):
        return {"title": "宫保鸡丁", "ingredients": ["鸡腿 2个"], "steps": [{"step": 1, "text": "切丁腌制"}]}

    async def get_ingredient_nutrition(self, keyword):
        return {"name": keyword, "calory": "118", "protein": "24.6"}


@pytest.mark.asyncio
async def test_holiday_calendar():
    r = await make_holiday_calendar(FakeDS()).ainvoke({"date": "2026-10-01"})
    assert r["holidays"][0]["name"] == "国庆节"


@pytest.mark.asyncio
async def test_holiday_calendar_requires_date_month_or_year():
    """holiday_calendar 的 date/month/year 三选一：都不传时返回错误提示而非抛异常。"""
    r = await make_holiday_calendar(FakeDS()).ainvoke({})
    assert "error" in r


@pytest.mark.asyncio
async def test_weather_query_default_days():
    """weather_query 的 days 有默认值：只传 city 不传 days 时不应抛 TypeError。"""
    res = await make_weather_query(FakeDS()).ainvoke({"city": "成都"})
    assert res[0]["day_weather"] == "晴"


@pytest.mark.asyncio
async def test_translate():
    r = await make_translate(FakeDS()).ainvoke({"text": "你好", "to_lang": "en"})
    assert r["translate"] == "HELLO"


@pytest.mark.asyncio
async def test_hotboard():
    r = await make_hotboard(FakeDS()).ainvoke({"type": "weibo"})
    assert r["list"][0]["title"] == "热搜1"


@pytest.mark.asyncio
async def test_recipe_search():
    r = await make_recipe_search(FakeDS()).ainvoke({"keyword": "宫保鸡丁"})
    assert r["items"][0]["title"] == "宫保鸡丁"


@pytest.mark.asyncio
async def test_recipe_detail():
    r = await make_recipe_detail(FakeDS()).ainvoke({"id": "333"})
    assert r["ingredients"][0] == "鸡腿 2个"


@pytest.mark.asyncio
async def test_ingredient_nutrition():
    r = await make_ingredient_nutrition(FakeDS()).ainvoke({"keyword": "鸡胸肉"})
    assert r["calory"] == "118"
