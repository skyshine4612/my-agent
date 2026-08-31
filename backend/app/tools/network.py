# app/tools/network.py
# 网络工具：Tavily 语义搜索 + 网页正文提取（替代原 Bing 搜索）。
from app.datasource.tavily import TavilyDataSource


def make_tavily_search(ds):
    """Tavily 搜索工具：语义搜索网页，返回标题/URL/摘要/相关度。"""

    async def tavily_search(query, max_results=5, search_depth="basic"):
        """用 Tavily 语义搜索网页，返回标题/链接/摘要。用于实时信息、新闻或通用知识查询。"""
        result = await ds.search(query, max_results=max_results, search_depth=search_depth)
        # 精简返回：只留 LLM 关心的字段，剔除原始正文噪音
        return {
            "query": result.get("query", query),
            "answer": result.get("answer"),
            "results": [
                {"title": r.get("title"), "url": r.get("url"), "content": r.get("content"), "score": r.get("score")}
                for r in result.get("results", [])
            ],
        }

    return tavily_search


def make_tavily_extract(ds):
    """Tavily 提取工具：从 URL 抓取网页正文（Markdown）。"""

    async def tavily_extract(urls, extract_depth="basic"):
        """从指定 URL 列表提取网页正文（Markdown），用于深入阅读搜索结果指向的页面。"""
        result = await ds.extract(urls, extract_depth=extract_depth)
        return {
            "results": [
                {"url": r.get("url"), "title": r.get("title"), "raw_content": r.get("raw_content")}
                for r in result.get("results", [])
            ],
            "failed_results": result.get("failed_results"),
        }

    return tavily_extract


def register_network_tools(registry):
    """把网络工具注入 ToolRegistry。"""
    tavily_ds = TavilyDataSource()  # 内联 new（httpx 客户端只在启动时创建一次）

    registry.register("tavily_search",
                      "用 Tavily 语义搜索网页，返回标题/链接/摘要/相关度分数。用于专用工具未覆盖的实时信息、新闻或通用知识查询。",
                      {"type": "object", "properties": {
                          "query": {"type": "string", "description": "搜索关键词"},
                          "max_results": {"type": "integer", "description": "返回结果数，默认 5，最大 20"},
                          "search_depth": {"type": "string", "description": "搜索深度：basic / advanced"},
                      }, "required": ["query"]},
                      make_tavily_search(tavily_ds), "联网搜索")

    registry.register("tavily_extract",
                      "从指定 URL 列表提取网页正文（Markdown）。用于深入阅读 tavily_search 返回的链接内容。",
                      {"type": "object", "properties": {
                          "urls": {"type": "array", "items": {"type": "string"},
                                   "description": "要提取的 URL 列表（最多 20 个）"},
                          "extract_depth": {"type": "string",
                                            "description": "提取深度：basic / advanced（JS 渲染页用 advanced）"},
                      }, "required": ["urls"]},
                      make_tavily_extract(tavily_ds), "网页正文提取")
