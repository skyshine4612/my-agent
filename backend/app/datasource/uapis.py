# datasource/uapis.py
"""UAPIS 数据源：节假日/翻译/热榜（uapis.cn，HTTP 直连）。"""
from app.config import settings
from app.datasource.http_base import HttpDataSource


def parse_weather_uapis(payload, days=None):
    """把 UAPIS weather 返回解析成每日天气列表（契约：date/week/day_weather/night_weather/day_temp/night_temp/wind）。

    UAPIS 的 forecast 数组从今天开始、最多 7 天，每项含 weather_day/weather_night（昼夜天气）、
    temp_max/temp_min（高低温）、wind_dir_day/wind_scale_day（风向风力）、humidity 等。
    """
    if not isinstance(payload, dict):
        return []
    forecast = payload.get("forecast", [])
    if not isinstance(forecast, list):
        return []
    result = []
    for f in forecast:
        if not isinstance(f, dict):
            continue
        result.append({
            "date": f.get("date", ""),
            "week": f.get("week", ""),
            "day_weather": f.get("weather_day", ""),
            "night_weather": f.get("weather_night", ""),
            "day_temp": f.get("temp_max"),
            "night_temp": f.get("temp_min"),
            "wind": f"{f.get('wind_dir_day', '')}{f.get('wind_scale_day', '')}",
            "humidity": f.get("humidity"),
        })
    if days:
        result = result[:days]
    return result


class UapisDataSource(HttpDataSource):
    """UAPIS 数据源：封装 uapis.cn 的节假日/翻译/热榜接口。

    认证：Authorization: Bearer <UAPIS_API_KEY>。
    """

    BASE_URL = "https://uapis.cn"

    def __init__(self):
        super().__init__(timeout=15.0)
        self._headers = {"Authorization": f"Bearer {settings.uapis_api_key}"}

    async def get_holiday_calendar(
            self,
            date: str = "",
            month: str = "",
            year: str = "",
            timezone: str = "Asia/Shanghai",
            holiday_type: str = "all",
            include_nearby: bool = False,
            nearby_limit: int = 7,
            exclude_past: bool = True,
    ) -> dict:
        """按天/月/年查询万年历与节假日信息（date/month/year 三选一）。"""
        params = {
            "date": date,
            "month": month,
            "year": year,
            "timezone": timezone,
            "holiday_type": holiday_type,
            # bool 显式转小写字符串，避免 httpx 序列化成 Python 的 "True"/"False"
            "include_nearby": "true" if include_nearby else "false",
            "nearby_limit": nearby_limit,
            "exclude_past": "true" if exclude_past else "false",
        }
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/misc/holiday-calendar", params=params, headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()

    async def translate(self, text: str, to_lang: str = "en") -> dict:
        """翻译文本（自动检测源语言），to_lang 为目标语言代码（en/zh/ja/ko 等）。"""
        resp = await self._client.post(
            f"{self.BASE_URL}/api/v1/translate/text",
            params={"to_lang": to_lang}, json={"text": text}, headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_hotboard(self, type: str, limit: int = 20) -> dict:
        """查询指定平台的热搜热榜（type 如 weibo/zhihu/baidu/bilibili）。"""
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/misc/hotboard",
            params={"type": type, "limit": limit}, headers=self._headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def search_recipe(self, keyword: str) -> dict:
        """按菜名/食材关键词搜索菜谱，返回菜谱列表（id/title）。"""
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/food/recipe",
            params={"keyword": keyword}, headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "keyword": data.get("keyword"),
            "items": [{"id": i.get("id"), "title": i.get("title")} for i in data.get("items", [])],
        }

    async def get_recipe_detail(self, recipe_id: str) -> dict:
        """按菜谱 id 查详情，返回食材用量清单 + 分步做法（不含图片）。"""
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/food/recipe/detail",
            params={"id": recipe_id}, headers=self._headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "title": data.get("title"),
            "ingredients": data.get("ingredients", []),
            "steps": [{"step": s.get("step"), "text": s.get("text")} for s in data.get("steps", [])],
        }

    async def get_ingredient_nutrition(self, keyword: str) -> dict:
        """查食材营养：先搜索拿 code，再查营养详情（热量/蛋白质/脂肪/碳水/膳食纤维/钠）。"""
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/food/ingredient",
            params={"keyword": keyword}, headers=self._headers,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return {"error": f"未找到食材「{keyword}」"}

        resp2 = await self._client.get(
            f"{self.BASE_URL}/api/v1/food/ingredient/detail",
            params={"code": items[0]["code"]}, headers=self._headers,
        )
        resp2.raise_for_status()
        data = resp2.json()
        return {
            "name": data.get("name"),
            "calory": data.get("calory"),  # 每 100 克热量（大卡）
            "protein": data.get("protein"),
            "fat": data.get("fat"),
            "carbohydrate": data.get("carbohydrate"),
            "fiber_dietary": data.get("fiber_dietary"),
            "natrium": data.get("natrium"),
            "health_light": data.get("health_light"),  # 健康评级（绿灯食物=1）
            "appraise": data.get("appraise"),
        }

    async def get_weather(self, city: str, days: int = 7) -> list[dict]:
        """查询城市未来天气（forecast=true，最多 7 天预报），city 支持中文名/英文名/adcode。

        天气接口免费无需鉴权，故不带 Authorization（与其它需鉴权的接口区分，实测免 key 可用）。
        """
        resp = await self._client.get(
            f"{self.BASE_URL}/api/v1/misc/weather",
            params={"city": city, "forecast": "true", "lang": "zh"},
        )
        resp.raise_for_status()
        return parse_weather_uapis(resp.json(), days)
