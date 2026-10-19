# app/tools/common.py
# 通用工具：节假日/农历 + 天气 + 翻译 + 热榜（通用能力，不绑定具体业务）。
# 工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
# 参数描述用 Annotated[T, Field(description=...)]：它是模型可见契约，裸注解会静默丢掉。
# 默认值一律照抄原函数签名（pydantic 由「有无默认值」推出 required，签名是唯一权威来源）。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.datasource.uapis import UapisDataSource
from app.tools.content import render_content


def make_holiday_calendar(ds):
    """节假日工具：查指定日期/月份/年份的节假日、农历、节气。"""

    @tool(response_format="content_and_artifact")
    async def holiday_calendar(
        date: Annotated[str, Field(description="按天查询，格式 YYYY-MM-DD")] = "",
        month: Annotated[str, Field(description="按月查询，格式 YYYY-MM")] = "",
        year: Annotated[str, Field(description="按年查询，格式 YYYY")] = "",
    ) -> tuple[str, dict]:
        """查询指定日期/月份/年份的节假日、农历、节气、休息日信息"""
        if not date and not month and not year:
            raw = {"error": "必须提供 date、month 或 year 参数之一"}
            return render_content(raw), raw
        result = await ds.get_holiday_calendar(date=date, month=month, year=year)
        raw = {
            "summary": result.get("summary"),
            "days": result.get("days"),
            "holidays": result.get("holidays"),
        }
        return render_content(raw), raw

    return holiday_calendar


def make_weather_query(ds):
    """天气查询工具：查城市未来 N 天天气（UAPIS 数据源，最多 7 天预报）。"""

    @tool(response_format="content_and_artifact")
    async def weather_query(
        city: Annotated[str, Field(description="城市名（中文/英文）或 adcode")],
        days: Annotated[int, Field(description="预报天数，最多 7 天")] = 7,
    ) -> tuple[str, list[dict]]:
        """查询城市未来天气（最多 7 天预报），city 支持中文名/英文名/adcode"""
        raw = await ds.get_weather(city, days)
        return render_content(raw), raw

    return weather_query


def make_translate(ds):
    """翻译工具：把文本翻译成目标语言。"""

    @tool(response_format="content_and_artifact")
    async def translate(
        text: Annotated[str, Field(description="要翻译的文本")],
        to_lang: Annotated[str, Field(description="目标语言代码，如 en(英语)/zh(中文)/ja(日语)/ko(韩语)，默认 en")] = "en",
    ) -> tuple[str, dict]:
        """翻译文本，自动检测源语言，翻译成目标语言"""
        raw = await ds.translate(text, to_lang)
        return render_content(raw), raw

    return translate


def make_hotboard(ds):
    """热榜工具：查指定平台热搜。"""

    @tool(response_format="content_and_artifact")
    async def hotboard(
        type: Annotated[str, Field(description="平台类型，如 weibo/zhihu/baidu/bilibili")],
        limit: Annotated[int, Field(description="返回条数，默认 20")] = 20,
    ) -> tuple[str, dict]:
        """查询指定平台的热搜热榜（微博/知乎/百度等）"""
        raw = await ds.get_hotboard(type, limit)
        return render_content(raw), raw

    return hotboard


def make_recipe_search(ds):
    """菜谱搜索工具：按菜名/食材搜菜谱。"""

    @tool(response_format="content_and_artifact")
    async def recipe_search(
        keyword: Annotated[str, Field(description="菜名或食材关键词，如 宫保鸡丁、鸡胸肉")],
    ) -> tuple[str, dict]:
        """按菜名或食材关键词搜索菜谱，返回菜谱列表（含 id）"""
        raw = await ds.search_recipe(keyword)
        return render_content(raw), raw

    return recipe_search


def make_recipe_detail(ds):
    """菜谱详情工具：查菜谱做法。"""

    @tool(response_format="content_and_artifact")
    async def recipe_detail(
        id: Annotated[str, Field(description="菜谱 id，来自 recipe_search 返回")],
    ) -> tuple[str, dict]:
        """调用前必须先调用 recipe_search，按 recipe_search 返回的 菜谱 id 查询菜谱详情，返回食材用量清单 + 分步做法"""
        raw = await ds.get_recipe_detail(id)
        return render_content(raw), raw

    return recipe_detail


def make_ingredient_nutrition(ds):
    """食材营养工具：查食材营养成分。"""

    @tool(response_format="content_and_artifact")
    async def ingredient_nutrition(
        keyword: Annotated[str, Field(description="食材名称，如 鸡胸肉、白菜")],
    ) -> tuple[str, dict]:
        """查询食材的营养成分（热量/蛋白质/脂肪/碳水/膳食纤维）"""
        raw = await ds.get_ingredient_nutrition(keyword)
        return render_content(raw), raw

    return ingredient_nutrition


def register_common_tools(registry):
    """把通用工具（节假日 / 翻译 / 热榜 / 菜谱 / 营养 / 天气）注入 ToolRegistry。

    全部通用能力（含天气）共用一个 UAPIS 数据源；高德数据源仅在旅行工具（travel.py）中使用。
    """
    uapis_ds = UapisDataSource()
    registry.register_tool(make_holiday_calendar(uapis_ds), "查节假日")
    registry.register_tool(make_translate(uapis_ds), "翻译")
    registry.register_tool(make_hotboard(uapis_ds), "查热榜")
    registry.register_tool(make_recipe_search(uapis_ds), "搜菜谱")
    registry.register_tool(make_recipe_detail(uapis_ds), "搜菜谱做法")
    registry.register_tool(make_ingredient_nutrition(uapis_ds), "搜食材营养")
    registry.register_tool(make_weather_query(uapis_ds), "查询天气")
    # 返回内联 new 的数据源（供上层统一 close）
    return [uapis_ds]
