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
    """variflight 机票数据源：封装 ModelScope variflight-MCP 的航班查询。"""

    def __init__(self):
        super().__init__(settings.flight_variflight_url, settings.modelscope_token)

    @staticmethod
    def _to_iata(city: str) -> str:
        """中文城市名 → IATA 3 字母城市代码；查不到时返回原值（可能是用户已给代码）。"""
        return CITY_IATA.get(city, city)

    async def search_flights(self, date: str, from_city: str, to_city: str) -> str:
        """查跨城机票，返回航班信息（航班号/航司/时间/机型 + 价格）。"""
        result = await self._call_tool("searchFlightsByDepArr", {
            "date": date,
            "depcity": self._to_iata(from_city),
            "arrcity": self._to_iata(to_city)})
        return self._extract_text(result)
