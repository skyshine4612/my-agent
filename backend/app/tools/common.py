# app/tools/common.py
# 通用工具：节假日/农历 + 天气 + 翻译 + 热榜（通用能力，不绑定具体业务）。
from app.datasource.uapis import UapisDataSource


def make_holiday_calendar(ds):
    """节假日工具：查指定日期/月份/年份的节假日、农历、节气。"""
    async def holiday_calendar(date="", month="", year=""):
        """查询指定日期/月份/年份的节假日、农历、节气、休息日信息（date/month/year 三选一）。"""
        if not date and not month and not year:
            return {"error": "必须提供 date、month 或 year 参数之一"}
        result = await ds.get_holiday_calendar(date=date, month=month, year=year)
        return {
            "summary": result.get("summary"),
            "days": result.get("days"),
            "holidays": result.get("holidays"),
        }
    return holiday_calendar


def make_weather_query(ds):
    """天气查询工具：查城市未来 N 天天气。"""
    async def weather_query(city, days=3):
        """查询指定城市未来 N 天（默认 3 天）的天气，返回天气列表。"""
        return await ds.get_weather(city, days)
    return weather_query


def make_translate(ds):
    """翻译工具：把文本翻译成目标语言。"""
    async def translate(text, to_lang="en"):
        """把文本翻译成目标语言（自动检测源语言）。to_lang 如 en/zh/ja/ko 等。"""
        return await ds.translate(text, to_lang)
    return translate


def make_hotboard(ds):
    """热榜工具：查指定平台热搜。"""
    async def hotboard(type, limit=20):
        """查询指定平台的热搜热榜（type 如 weibo/zhihu/baidu/bilibili）。"""
        return await ds.get_hotboard(type, limit)
    return hotboard


def make_recipe_search(ds):
    """菜谱搜索工具：按菜名/食材搜菜谱。"""
    async def recipe_search(keyword):
        """按菜名或食材关键词搜索菜谱，返回菜谱列表（含 id，供 recipe_detail 查详情）。"""
        return await ds.search_recipe(keyword)
    return recipe_search


def make_recipe_detail(ds):
    """菜谱详情工具：查菜谱做法。"""
    async def recipe_detail(id):
        """按菜谱 id 查询菜谱详情，返回食材用量清单 + 分步做法。"""
        return await ds.get_recipe_detail(id)
    return recipe_detail


def make_ingredient_nutrition(ds):
    """食材营养工具：查食材营养成分。"""
    async def ingredient_nutrition(keyword):
        """查询食材营养成分（热量/蛋白质/脂肪/碳水/膳食纤维），用于计算一餐热量。"""
        return await ds.get_ingredient_nutrition(keyword)
    return ingredient_nutrition


def register_common_tools(registry, amap_web_ds):
    """把通用工具（节假日 / 翻译 / 热榜 / 菜谱 / 营养 / 天气）注入 ToolRegistry。

    节假日/翻译/热榜/菜谱/营养共用一个 UAPIS 数据源；weather_query 用传入的高德 Web API 数据源。
    """
    uapis_ds = UapisDataSource()
    registry.register("holiday_calendar", "查询指定日期/月份/年份的节假日、农历、节气、休息日信息",
        {"type": "object", "properties": {
            "date": {"type": "string", "description": "按天查询，格式 YYYY-MM-DD"},
            "month": {"type": "string", "description": "按月查询，格式 YYYY-MM"},
            "year": {"type": "string", "description": "按年查询，格式 YYYY"},
        }, "required": []},
        make_holiday_calendar(uapis_ds), "查节假日")
    registry.register("translate", "翻译文本，自动检测源语言，翻译成目标语言",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "要翻译的文本"},
            "to_lang": {"type": "string", "description": "目标语言代码，如 en(英语)/zh(中文)/ja(日语)/ko(韩语)，默认 en"},
        }, "required": ["text"]},
        make_translate(uapis_ds), "翻译")
    registry.register("hotboard", "查询指定平台的热搜热榜（微博/知乎/百度等）",
        {"type": "object", "properties": {
            "type": {"type": "string", "description": "平台类型，如 weibo/zhihu/baidu/bilibili"},
            "limit": {"type": "integer", "description": "返回条数，默认 20"},
        }, "required": ["type"]},
        make_hotboard(uapis_ds), "查热榜")
    registry.register("recipe_search", "按菜名或食材关键词搜索菜谱，返回菜谱列表（含 id）",
        {"type": "object", "properties": {"keyword": {"type": "string", "description": "菜名或食材关键词，如 宫保鸡丁、鸡胸肉"}}, "required": ["keyword"]},
        make_recipe_search(uapis_ds), "搜菜谱")
    registry.register("recipe_detail", "按菜谱 id 查询菜谱详情，返回食材用量清单 + 分步做法",
        {"type": "object", "properties": {"id": {"type": "string", "description": "菜谱 id，来自 recipe_search 返回"}}, "required": ["id"]},
        make_recipe_detail(uapis_ds), "菜谱做法")
    registry.register("ingredient_nutrition", "查询食材的营养成分（热量/蛋白质/脂肪/碳水/膳食纤维）",
        {"type": "object", "properties": {"keyword": {"type": "string", "description": "食材名称，如 鸡胸肉、白菜"}}, "required": ["keyword"]},
        make_ingredient_nutrition(uapis_ds), "食材营养")
    registry.register("weather_query", "查询城市未来天气",
        {"type": "object", "properties": {"city": {"type": "string"}, "days": {"type": "integer"}}, "required": ["city"]},
        make_weather_query(amap_web_ds), "查询天气")
