# datasource/uapis.py
"""UAPIS 数据源：节假日/翻译/热榜（uapis.cn，HTTP 直连）。"""
from app.config import settings
from app.datasource.http_base import HttpDataSource


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
            "calory": data.get("calory"),            # 每 100 克热量（大卡）
            "protein": data.get("protein"),
            "fat": data.get("fat"),
            "carbohydrate": data.get("carbohydrate"),
            "fiber_dietary": data.get("fiber_dietary"),
            "natrium": data.get("natrium"),
            "health_light": data.get("health_light"),  # 健康评级（绿灯食物=1）
            "appraise": data.get("appraise"),
        }
