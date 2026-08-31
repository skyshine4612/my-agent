# datasource/flight_variflight.py
"""variflight 机票数据源：查询跨城航班与票价。"""
from app.config import settings
from app.datasource.mcp_base import McpDataSource


# 常见城市中文名 → IATA 3 字母城市代码（variflight 的 searchFlightsByDepArr 需要城市代码）
CITY_IATA = {
    "北京": "BJS", "上海": "SHA", "广州": "CAN", "深圳": "SZX",
    "成都": "CTU", "重庆": "CKG", "杭州": "HGH", "西安": "SIA",
    "武汉": "WUH", "南京": "NKG", "长沙": "CSX", "昆明": "KMG",
    "厦门": "XMN", "青岛": "TAO", "天津": "TSN", "郑州": "CGO",
}


class FlightVariflightDataSource(McpDataSource):
    """variflight 机票数据源：直连 variflight 官方 MCP（不走 ModelScope 代理）。

    认证用 X-API-Key header（区别于 ModelScope 托管 MCP 的 Bearer 令牌）。
    """

    def __init__(self):
        super().__init__(settings.flight_variflight_url, headers={"X-API-Key": settings.variflight_api_key})

    @staticmethod
    def _to_iata(city: str) -> str:
        """中文城市名 → IATA 3 字母城市代码；查不到时返回原值（可能是用户已给代码）。"""
        return CITY_IATA.get(city, city)

    async def search_flights(self, date: str, from_city: str, to_city: str) -> str:
        """查跨城机票（含舱位价格），用 getFlightPriceByCities 返回航班号/时间 + 各舱位票价。"""
        result = await self._call_tool("getFlightPriceByCities", {
            "dep_city": self._to_iata(from_city),
            "arr_city": self._to_iata(to_city),
            "dep_date": date})
        return self._extract_text(result)
