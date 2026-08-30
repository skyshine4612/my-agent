# app/tools/news.py
# 新闻工具：每日热点新闻 / 新闻搜索 / 新闻分类（ModelScope news-daily MCP）。
from app.datasource.news_mcp import NewsMcpDataSource


def register_news_tools(registry):
    """把新闻工具注入 ToolRegistry。"""
    news_ds = NewsMcpDataSource()   # 内联 new（httpx 客户端只在启动时创建一次）

    async def get_hot_news(category: str = "general", limit: int = 10) -> str:
        """获取热点新闻。"""
        return await news_ds.get_hot_news(category, limit)

    async def search_news(keyword: str, limit: int = 10) -> str:
        """按关键词搜索新闻。"""
        return await news_ds.search_news(keyword, limit)

    async def list_news_categories() -> str:
        """列出可用的新闻分类。"""
        return await news_ds.list_categories()

    get_hot_news.__name__ = "get_hot_news"
    get_hot_news.description = "获取每日热点新闻（category: general 综合 / tech 科技 / international 国际）"
    get_hot_news.parameters = {
        "type": "object",
        "properties": {
            "category": {"type": "string", "description": "新闻分类", "enum": ["general", "tech", "international"]},
            "limit": {"type": "integer", "description": "返回条数，1-20，默认 10"},
        },
    }

    search_news.__name__ = "search_news"
    search_news.description = "按关键词搜索新闻"
    search_news.parameters = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "返回条数，1-10，默认 10"},
        },
        "required": ["keyword"],
    }

    list_news_categories.__name__ = "list_news_categories"
    list_news_categories.description = "列出可用的新闻分类"
    list_news_categories.parameters = {"type": "object", "properties": {}}

    registry.register("get_hot_news", get_hot_news.description, get_hot_news.parameters, get_hot_news)
    registry.register("search_news", search_news.description, search_news.parameters, search_news)
    registry.register("list_news_categories", list_news_categories.description, list_news_categories.parameters, list_news_categories)
