# tests/test_network_tools.py
# Tavily 网络工具工厂的契约测试：用 FakeDS 验证注入与调用（不触碰真实 HTTP）。
import pytest

from app.tools.network import make_tavily_search, make_tavily_extract


class FakeTavilyDS:
    """Tavily 假数据源：返回固定搜索结果与网页正文。"""

    async def search(self, query, max_results=5, search_depth="basic", **kw):
        return {
            "query": query, "answer": None,
            "results": [{"title": "北京旅游", "url": "https://x.com", "content": "攻略", "score": 0.9}],
        }

    async def extract(self, urls, extract_depth="basic", **kw):
        return {
            "results": [{"url": urls[0], "title": "t", "raw_content": "正文"}],
            "failed_results": [],
        }


@pytest.mark.asyncio
async def test_tavily_search_strips_raw_fields():
    """tavily_search 精简返回：只留 title/url/content/score，剔除原始正文。"""
    fn = make_tavily_search(FakeTavilyDS())
    r = await fn(query="北京旅游")
    assert r["results"][0]["title"] == "北京旅游"
    assert r["results"][0]["url"] == "https://x.com"


@pytest.mark.asyncio
async def test_tavily_extract():
    fn = make_tavily_extract(FakeTavilyDS())
    r = await fn(urls=["https://x.com"])
    assert r["results"][0]["raw_content"] == "正文"
