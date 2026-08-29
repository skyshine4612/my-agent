# app/datasource/amap_mcp.py
# 高德地图数据源实现：通过 mcp SDK 连接 ModelScope 托管的高德 MCP 服务（streamable HTTP），
# 把 DataSource 契约方法（search_poi/get_weather/plan_route/geocode）映射到高德 MCP 工具，
# 并将返回结果统一解析成 DataSource 契约结构。
import asyncio
import json
import logging

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client, create_mcp_http_client

from app.config import settings
from app.datasource.base import DataSource

logger = logging.getLogger(__name__)


# ---- 工具名映射（纯函数，便于单测，不依赖网络） ----

# 契约方法 → 高德 MCP 工具名的静态映射
AMAP_TOOL_MAP = {
    "search_poi": "maps_text_search",   # 地点关键字搜索
    "get_weather": "maps_weather",       # 天气查询
    "geocode": "maps_geo",               # 地理编码（地址 → 经纬度）
}

# plan_route 按出行方式映射到不同的高德路径规划工具
ROUTE_TOOL_MAP = {
    "walking": "maps_direction_walking",            # 步行
    "driving": "maps_direction_driving",            # 驾车
    "transit": "maps_direction_transit_integrated",  # 公交/地铁综合
}


def route_tool_for_mode(mode):
    """按出行方式返回对应的高德路径规划工具名；未识别的方式兜底为驾车。"""
    return ROUTE_TOOL_MAP.get(mode, ROUTE_TOOL_MAP["driving"])


def resolve_tool_name(action, mode=None):
    """把 DataSource 契约方法名解析成高德 MCP 实际工具名。

    action: search_poi / get_weather / plan_route / geocode
    mode:   仅 plan_route 需要，取值 walking / driving / transit
    """
    if action == "plan_route":
        return route_tool_for_mode(mode)
    return AMAP_TOOL_MAP[action]


# ---- 结果解析（纯函数，把高德返回转成 DataSource 契约结构） ----

def _coerce_location(loc):
    """把高德返回的 location 统一转成 {"lng": float, "lat": float}。

    高德接口的 location 多为 "lng,lat" 字符串（如 "116.397,39.908"）；
    兼容已是 dict（含 lng/lat）的情况，无法解析时兜底为 0。
    """
    if isinstance(loc, dict):
        return {"lng": float(loc.get("lng", 0)), "lat": float(loc.get("lat", 0))}
    if isinstance(loc, str) and "," in loc:
        lng, lat = loc.split(",", 1)
        return {"lng": float(lng), "lat": float(lat)}
    return {"lng": 0.0, "lat": 0.0}


def _extract_photo(p):
    """从 POI dict 提取图片 URL，兼容两种返回格式。

    ModelScope 托管版返回 photos（dict，图片在 photos.url）；
    高德官方版返回 photo（单数字符串）。统一提取成字符串 URL，缺失时返回空串。
    """
    if not isinstance(p, dict):
        return ""
    photos = p.get("photos")
    if isinstance(photos, dict):
        return photos.get("url", "") or ""
    photo = p.get("photo")
    if isinstance(photo, str):
        return photo
    return ""


def parse_poi_list(payload):
    """把 maps_text_search 的返回解析成 POI 列表（契约：name/address/location/category/price）。

    注意：ModelScope 托管的高德 MCP 的 maps_text_search 返回的 POI 不含经纬度 location
    字段（实际只有 id/name/address/typecode/photos），location 暂兜底为 0；经纬度需用
    maps_search_detail（按 POI id）补充，属后续优化。价格 biz_ext.cost 当前也不返回，price 兜底 0。
    """
    pois = payload.get("pois", []) if isinstance(payload, dict) else []
    result = []
    for p in pois:
        # biz_ext.cost 可能携带价格信息，当前 ModelScope 版未返回，缺省按 0
        biz_ext = p.get("biz_ext") or {}
        cost = biz_ext.get("cost")
        try:
            price = float(cost) if cost not in (None, "") else 0.0
        except (TypeError, ValueError):
            price = 0.0
        result.append({
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "address": p.get("address", ""),
            "location": _coerce_location(p.get("location")),
            "category": p.get("typecode", ""),   # 高德用 typecode 字段表示 POI 分类（不是 type）
            "price": price,
            "photo": _extract_photo(p),   # 高德返回的图片 URL（供前端展示）
        })
    return result


def _coerce_int(value, default=0):
    """把高德返回的温度字段（通常为字符串数字，如 "31"）安全转成 int，缺失/异常时兜底为 default。"""
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_weather(payload):
    """把 maps_weather 的返回解析成每日天气列表（契约：date/day_weather/day_temp/night_temp）。

    高德 maps_weather 的预报数据在 forecasts[].casts[] 中，每条 cast 除白天天气外还带
    daytemp/nighttemp 温度字段。此前解析只返回 date/day_weather，导致前端 WeatherPanel
    渲染 w.day_temp / w.night_temp 永远为 undefined，这里补出温度字段；温度缺失时兜底 0。
    """
    if not isinstance(payload, dict):
        return []
    result = []
    # 预报数据在 forecasts[].casts[] 中，每条 cast 是某一天的天气
    for f in payload.get("forecasts", []):
        for c in f.get("casts", []):
            result.append({
                "date": c.get("date", ""),
                "day_weather": c.get("dayweather", ""),   # 高德白天天气字段 → 契约 day_weather
                "day_temp": _coerce_int(c.get("daytemp")),     # 高德白天温度 → 契约 day_temp
                "night_temp": _coerce_int(c.get("nighttemp")), # 高德夜间温度 → 契约 night_temp
            })
    return result


def parse_route(payload):
    """把 maps_direction_* 的返回解析成路径规划结果 dict（含起终点/距离/耗时）。"""
    if not isinstance(payload, dict):
        return {}
    route = payload.get("route", {}) or {}
    return {
        "origin": route.get("origin", ""),
        "destination": route.get("destination", ""),
        "distance": route.get("distance", ""),
        "duration": route.get("duration", ""),
    }


def parse_geocode(payload):
    """把 maps_geo 的返回解析成 {"lng": float, "lat": float}。"""
    if not isinstance(payload, dict):
        return {"lng": 0.0, "lat": 0.0}
    geocodes = payload.get("geocodes", [])
    if geocodes:
        return _coerce_location(geocodes[0].get("location"))
    return {"lng": 0.0, "lat": 0.0}


class AmapMcpDataSource(DataSource):
    """高德地图数据源：通过 mcp SDK 连 ModelScope 托管的高德 MCP 服务（streamable HTTP）取数。

    每次调用 _call_tool 都会新建连接 → 初始化会话 → 调工具 → 断开，
    避免长连接状态污染；真实连接逻辑结构正确，但网络连通性依赖端到端冒烟验证。
    """

    def __init__(self):
        # 连接参数：MCP 服务地址 + ModelScope 令牌（放入 Authorization 请求头）
        self.url = settings.amap_mcp_url
        self.headers = {"Authorization": f"Bearer {settings.modelscope_token}"}
        # streamable HTTP 协议：headers 需通过 http_client 传入（streamable_http_client 本身无 headers 参数）
        self.http_client = create_mcp_http_client(headers=self.headers)
        # 懒加载的持久 session：首次调用时建立连接+initialize，后续调用复用（省去重复握手）
        self._session = None
        self._exit_stack = None
        self._lock = asyncio.Lock()  # 保护懒加载 session 的并发安全（多个子 Agent 并行调用工具时）

    async def _ensure_session(self):
        """确保有可用的持久 session：首次调用建立连接并 initialize，连接失效时重建。

        用 AsyncExitStack 统一管理连接与 session 的生命周期（aclose 时一并退出）；
        用 asyncio.Lock 保证并发下 initialize 只发生一次（多个子 Agent 并行调用工具时）。
        """
        async with self._lock:
            if self._session is not None:
                return self._session
            from contextlib import AsyncExitStack
            self._exit_stack = AsyncExitStack()
            # 进入 streamable_http_client（返回 (read, write) 读写流）
            read, write = await self._exit_stack.enter_async_context(
                streamable_http_client(self.url, http_client=self.http_client))
            # 进入 ClientSession（返回 session 对象）
            self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            return self._session

    async def _call_tool(self, tool_name, arguments):
        """调用指定工具：复用持久 session（避免每次调用都重新 initialize）。"""
        # 日志：记录对高德 MCP 的实际工具调用（名称 + 参数），便于追踪数据源层做了什么
        logger.info("[高德MCP] 调 %s，参数 %s", tool_name, json.dumps(arguments, ensure_ascii=False))
        session = await self._ensure_session()
        try:
            result = await session.call_tool(tool_name, arguments)
            logger.info("[高德MCP] %s 返回成功", tool_name)
            return result
        except Exception:
            # 连接失效：清理缓存的 session 与连接，下次调用自动重建
            logger.warning("[高德MCP] %s 调用失败，将重建连接", tool_name, exc_info=True)
            if self._exit_stack is not None:
                await self._exit_stack.aclose()
                self._exit_stack = None
                self._session = None
            raise

    async def close(self):
        """关闭持久 session 与连接（进程退出前调用）。

        注：懒加载 session 可能在 gather 子 task 里进入（enter），而 close 在别的 task 里
        退出（exit），anyio 的 cancel scope 要求同一 task 进出会抛 RuntimeError；这里吞掉该
        异常——进程退出时 OS 会兜底清理连接，不影响功能。
        """
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except Exception:
                pass
            self._exit_stack = None
            self._session = None

    @staticmethod
    def _extract_payload(result):
        """从 CallToolResult 中取出结构化 payload（优先 structured_content，兜底解析文本块）。"""
        sc = getattr(result, "structured_content", None)
        if sc is not None:
            return sc
        # 兜底：把 content 里所有文本块拼接后尝试 JSON 解析；解析失败返回空 dict
        text = "".join(getattr(b, "text", "") for b in getattr(result, "content", []))
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {}

    async def search_poi(self, keywords, city, **kw):
        """地点关键字搜索：调 maps_text_search，返回 POI 列表（契约结构）。

        简化：不再逐个 POI 补 maps_search_detail 的坐标（额外的 N+1 调用拖慢且易失败），
        直接返回 parse_poi_list 的解析结果；maps_text_search 本身不含经纬度，location 兜底为 0。
        """
        args = {"keywords": keywords, "city": city, **kw}
        result = await self._call_tool(resolve_tool_name("search_poi"), args)
        return parse_poi_list(self._extract_payload(result))

    async def get_weather(self, city, days):
        """天气查询：调 maps_weather，返回每日天气列表（契约结构）。"""
        args = {"city": city}
        result = await self._call_tool(resolve_tool_name("get_weather"), args)
        return parse_weather(self._extract_payload(result))

    async def plan_route(self, origin, dest, mode):
        """路径规划：按 mode 选工具，调对应 maps_direction_*，返回规划结果 dict。"""
        tool = resolve_tool_name("plan_route", mode)
        args = {"origin": origin, "destination": dest}
        result = await self._call_tool(tool, args)
        payload = parse_route(self._extract_payload(result))
        payload["mode"] = mode   # 回填出行方式，便于下游识别
        return payload

    async def geocode(self, address):
        """地理编码：调 maps_geo，返回 {"lng", "lat"}。"""
        args = {"address": address}
        result = await self._call_tool(resolve_tool_name("geocode"), args)
        return parse_geocode(self._extract_payload(result))
