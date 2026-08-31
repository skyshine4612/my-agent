# datasource/tavily.py
"""Tavily 数据源：语义搜索 + 网页正文提取（api.tavily.com，HTTP 直连）。"""
from app.config import settings
from app.datasource.http_base import HttpDataSource


class TavilyDataSource(HttpDataSource):
    """Tavily 数据源：封装 search / extract 两个接口。

    认证：api_key 放进请求 body（Tavily REST API 约定）。
    """

    BASE_URL = "https://api.tavily.com"

    def __init__(self):
        super().__init__(timeout=30.0)
        self._api_key = settings.tavily_api_key

    async def search(
        self,
        query: str,
        max_results: int = 5,
        search_depth: str = "basic",
        time_range: str | None = None,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        include_answer: bool = False,
        include_raw_content: bool = False,
    ) -> dict:
        """网络搜索，返回结构化结果（标题/URL/摘要/相关度分数）。"""
        body: dict = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
        }
        if time_range is not None:
            body["time_range"] = time_range
        if include_domains is not None:
            body["include_domains"] = include_domains
        if exclude_domains is not None:
            body["exclude_domains"] = exclude_domains
        resp = await self._client.post(f"{self.BASE_URL}/search", json=body)
        resp.raise_for_status()
        return resp.json()

    async def extract(
        self,
        urls: list[str],
        extract_depth: str = "basic",
        query: str | None = None,
        chunks_per_source: int | None = None,
    ) -> dict:
        """从 URL 列表提取网页正文（Markdown 格式）。"""
        body: dict = {
            "api_key": self._api_key,
            "urls": urls,
            "extract_depth": extract_depth,
        }
        if query is not None:
            body["query"] = query
        if chunks_per_source is not None:
            body["chunks_per_source"] = chunks_per_source
        resp = await self._client.post(f"{self.BASE_URL}/extract", json=body)
        resp.raise_for_status()
        return resp.json()
