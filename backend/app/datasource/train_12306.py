# datasource/train_12306.py
"""12306 火车票数据源：查询跨城火车票（车次/时间/历时/票价/余票）。"""
from app.config import settings
from app.datasource.mcp_base import McpDataSource


class Train12306DataSource(McpDataSource):
    """12306 火车票数据源：封装 ModelScope 12306-MCP 的 get-tickets 查询。"""

    def __init__(self):
        super().__init__(settings.train_12306_url, settings.modelscope_token)

    async def search_tickets(self, date: str, from_city: str, to_city: str) -> str:
        """查跨城火车票，返回 text 格式的票务信息。

        from_city/to_city 直接用中文城市名（12306-MCP 支持中文名或 station_code）。
        """
        result = await self._call_tool("get-tickets", {
            "date": date, "fromStation": from_city, "toStation": to_city, "format": "text"})
        return self._extract_text(result)
