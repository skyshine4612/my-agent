# datasource/amap_web.py
"""高德 Web 服务 API 数据源：直连 restapi.amap.com，提供 POI 搜索 / 天气。

统一用高德官方 Web 服务 API（一个 AMAP_API_KEY）。地理编码 / 附近搜索 / 路线规划等
坐标能力暂未接入（旅行规划用不到精确导航），做本地生活业务时再补。
"""
from app.config import settings
from app.datasource.http_base import HttpDataSource


# ---- 结果解析（纯函数，便于单测，不依赖网络） ----


def _coerce_location(loc):
    """把高德返回的 location 统一转成 {"lng": float, "lat": float}。

    高德接口的 location 多为 "lng,lat" 字符串；兼容已是 dict 的情况，无法解析时兜底 0。
    """
    if isinstance(loc, dict):
        return {"lng": float(loc.get("lng", 0)), "lat": float(loc.get("lat", 0))}
    if isinstance(loc, str) and "," in loc:
        lng, lat = loc.split(",", 1)
        try:
            return {"lng": float(lng), "lat": float(lat)}
        except ValueError:
            return {"lng": 0.0, "lat": 0.0}
    return {"lng": 0.0, "lat": 0.0}


def _coerce_int(value, default=0):
    """把高德返回的温度字段（通常为字符串数字）安全转成 int，缺失/异常时兜底 default。"""
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_photo(p):
    """从 POI 提取图片 URL（高德官方 photos 是列表，取第一张 url）。"""
    if not isinstance(p, dict):
        return ""
    photos = p.get("photos")
    if isinstance(photos, list) and photos:
        return photos[0].get("url", "") or ""
    if isinstance(photos, dict):
        return photos.get("url", "") or ""
    photo = p.get("photo")
    if isinstance(photo, str):
        return photo
    return ""


def parse_poi_list(payload):
    """把 place/text 的返回解析成 POI 列表（契约：name/address/location/category/price）。"""
    if not isinstance(payload, dict):
        return []
    result = []
    for p in payload.get("pois", []):
        biz_ext = p.get("biz_ext") or {}
        cost = biz_ext.get("cost")
        if isinstance(cost, (list, tuple)):  # 高德 biz_ext.cost 有时是数组
            cost = cost[0] if cost else None
        try:
            price = float(cost) if cost not in (None, "") else 0.0
        except (TypeError, ValueError):
            price = 0.0
        result.append({
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "address": p.get("address", ""),
            "location": _coerce_location(p.get("location")),
            "category": p.get("type", ""),   # 高德官方用 type 字段表示分类文本
            "price": price,
            "photo": _extract_photo(p),
        })
    return result


def parse_weather(payload, days=None):
    """把 weather extensions=all 的返回解析成每日天气列表（契约：date/day_weather/day_temp/night_temp）。

    高德官方 weather 返回 forecasts[0].casts[]（forecasts 是城市数组，casts 是每天）。
    """
    if not isinstance(payload, dict):
        return []
    forecasts = payload.get("forecasts", [])
    if not forecasts or not isinstance(forecasts[0], dict):
        return []
    result = []
    for c in forecasts[0].get("casts", []):
        result.append({
            "date": c.get("date", ""),
            "day_weather": c.get("dayweather", ""),
            "day_temp": _coerce_int(c.get("daytemp")),
            "night_temp": _coerce_int(c.get("nighttemp")),
        })
    if days:
        result = result[:days]
    return result


class AmapWebDataSource(HttpDataSource):
    """高德 Web 服务数据源：直连 restapi.amap.com 的 POI 搜索 / 天气。"""

    BASE_URL = "https://restapi.amap.com"

    def __init__(self):
        super().__init__(timeout=15.0)
        self._key = settings.amap_api_key

    async def _get(self, endpoint: str, params: dict) -> dict:
        params["key"] = self._key
        resp = await self._client.get(f"{self.BASE_URL}{endpoint}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def search_poi(self, keywords: str, city: str | None = None, **kw) -> list[dict]:
        """按关键字搜索 POI（place/text）。"""
        params = {"keywords": keywords, "output": "json", "offset": 20, "extensions": "base"}
        if city:
            params["city"] = city
        data = await self._get("/v3/place/text", params)
        return parse_poi_list(data)

    async def get_weather(self, city: str, days: int = 3) -> list[dict]:
        """查询城市未来天气（weather extensions=all，city 支持中文名或 adcode）。"""
        data = await self._get("/v3/weather/weatherInfo", {"city": city, "extensions": "all"})
        return parse_weather(data, days)
