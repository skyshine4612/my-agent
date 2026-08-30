# app/datasource/bing_mcp.py
# Bing 搜索数据源：封装 ModelScope bing-cn-mcp 的 bing_search（网页搜索）。
import json

from app.config import settings
from app.datasource.mcp_base import McpDataSource


class BingMcpDataSource(McpDataSource):
    """Bing 搜索数据源：封装 bing-cn-mcp 的网页搜索。"""

    def __init__(self):
        super().__init__(settings.bing_mcp_url, settings.modelscope_token)

    async def search(self, query: str, count: int = 10) -> str:
        """网页搜索：返回「标题 + 链接 + 摘要」格式化的文本列表，供 LLM 直接引用。

        bing MCP 的 bing_search 返回 content 文本块（JSON 字符串），而非 structured_content。
        """
        result = await self._call_tool("bing_search", {"query": query, "count": count})
        text = self._extract_text(result)
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text   # 非 JSON 时原样返回
        if not isinstance(data, dict):
            return text
        results = data.get("results", [])
        if not results:
            return "未找到结果"
        lines = []
        for r in results:
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("snippet", "")
            lines.append(f"- {title}\n  {url}\n  {snippet}")
        return "\n".join(lines)
