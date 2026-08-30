# app/datasource/news_mcp.py
# 每日新闻数据源：封装 ModelScope news-daily MCP（热点新闻/新闻搜索/分类）。
import json

from app.config import settings
from app.datasource.mcp_base import McpDataSource


class NewsMcpDataSource(McpDataSource):
    """每日新闻数据源：封装 news-daily MCP 的新闻查询。"""

    def __init__(self):
        super().__init__(settings.news_mcp_url, settings.modelscope_token)

    def _extract_result(self, result) -> str:
        """news MCP 返回 {"result": "..."}，提取 result 字段（纯文本）；解析失败原样返回。"""
        text = self._extract_text(result)
        try:
            data = json.loads(text)
            if isinstance(data, dict) and "result" in data:
                return str(data["result"])
        except (json.JSONDecodeError, TypeError):
            pass
        return text

    async def get_hot_news(self, category: str = "general", limit: int = 10) -> str:
        """获取热点新闻（category: general 综合 / tech 科技 / international 国际）。"""
        result = await self._call_tool("get_hot_news", {"category": category, "limit": limit})
        return self._extract_result(result)

    async def search_news(self, keyword: str, limit: int = 10) -> str:
        """按关键词搜索新闻。"""
        result = await self._call_tool("search_news", {"keyword": keyword, "limit": limit})
        return self._extract_result(result)

    async def list_categories(self) -> str:
        """列出可用的新闻分类。"""
        result = await self._call_tool("list_news_categories", {})
        return self._extract_result(result)
