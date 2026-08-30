# app/tools/network.py
# 网络工具：bing 网页搜索（ModelScope bing-cn-mcp）。
from app.datasource.bing_mcp import BingMcpDataSource


def register_network_tools(registry):
    """把网络工具注入 ToolRegistry。"""
    bing_ds = BingMcpDataSource()   # 内联 new（httpx 客户端只在启动时创建一次）

    async def bing_search(query: str, count: int = 10) -> str:
        """用必应搜索网页，返回标题/链接/摘要。"""
        return await bing_ds.search(query, count)

    bing_search.__name__ = "bing_search"
    bing_search.description = "用必应搜索网页，返回标题/链接/摘要。用于专用工具未覆盖的实时信息、新闻或通用知识查询。"
    bing_search.parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "description": "返回结果数，默认 10，最大 50"},
        },
        "required": ["query"],
    }
    registry.register("bing_search", bing_search.description, bing_search.parameters, bing_search, "联网搜索")
