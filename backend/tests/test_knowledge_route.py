# tests/test_knowledge_route.py
# 建库路由的失败路径：入库异常必须留日志，且外发的错误文本要按值脱敏；
# 抓到的正文过短不入库。直接测 helper 与路由函数，不经过 HTTP 层与数据库。
import logging

import pytest
from fastapi import HTTPException

from app.api.routes import knowledge as route
from app.config import settings


class _Boom:
    """入库时抛异常的知识库替身。"""

    def __init__(self, message: str):
        self._message = message

    async def ingest(self, *args, **kwargs):
        raise RuntimeError(self._message)


def _boom(monkeypatch, message: str) -> None:
    monkeypatch.setattr(route.service, "knowledge", _Boom(message))


@pytest.mark.asyncio
async def test_ingest_failure_returns_502_and_logs(monkeypatch, caplog):
    """入库失败抛 502 并打进日志——失败不再无声无息。"""
    _boom(monkeypatch, "embedding 服务不可用")
    with caplog.at_level(logging.ERROR, logger="app.api.routes.knowledge"):
        with pytest.raises(HTTPException) as ei:
            await route._ingest("u1", "正文", title="t", source_url="http://x")

    assert ei.value.status_code == 502
    assert any("入库失败" in r.getMessage() for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "应带异常栈，否则无从定位"


@pytest.mark.asyncio
async def test_ingest_failure_redacts_secrets(monkeypatch):
    """错误文本里的凭据按值抹掉后再外发，不随响应泄给前端。"""
    monkeypatch.setattr(settings, "supabase_jwt_secret", "test-secret-value")
    _boom(monkeypatch, "连接失败 test-secret-value")

    with pytest.raises(HTTPException) as ei:
        await route._ingest("u1", "正文", title="t", source_url="http://x")

    assert "test-secret-value" not in ei.value.detail
    assert "***" in ei.value.detail


def _fake_fetch(monkeypatch, content: str) -> None:
    async def fetch(url):
        return "标题", content

    monkeypatch.setattr(route, "fetch_url_text", fetch)


@pytest.mark.asyncio
async def test_short_page_is_rejected(monkeypatch):
    """限流 JSON 这类「抓到了但没内容」的页面要挡在库外。

    知乎限流时的实际输出，117 字——比旧的 50 字阈值长，所以只有抬高阈值才拦得住；
    而它压根不是正文。改动阈值时这条会跟着红/绿，别把它改短。
    """
    _fake_fetch(monkeypatch, '{"error":{"message":"您当前请求存在异常，暂时限制本次访问。'
                            '如有疑问，您可以通过手机摇一摇或登录后私信知乎小管家反馈。'
                            'a930cf82b3320a3c85888091e46f2d3a","code":40362}}')

    with pytest.raises(HTTPException) as ei:
        await route.ingest_url(route.UrlIn(url="https://www.zhihu.com/question/1"), user_id="u1")

    assert ei.value.status_code == 400
    assert "有效正文" in ei.value.detail


@pytest.mark.asyncio
async def test_short_page_never_reaches_ingest(monkeypatch):
    """被拦下的页面不能碰入库——否则噪声已经写进 documents 了才报错。"""
    _fake_fetch(monkeypatch, "太短")
    called = False

    async def ingest(*args, **kwargs):
        nonlocal called
        called = True
        return 1

    monkeypatch.setattr(route, "_ingest", ingest)

    with pytest.raises(HTTPException):
        await route.ingest_url(route.UrlIn(url="https://example.com/a"), user_id="u1")

    assert called is False


@pytest.mark.asyncio
async def test_page_at_threshold_is_ingested(monkeypatch):
    """阈值边界：刚好达标放行，少一个字拦下。"""
    called = {}

    async def ingest(user_id, content, title, source_url):
        called["content"] = content
        return 3

    monkeypatch.setattr(route, "_ingest", ingest)

    _fake_fetch(monkeypatch, "正" * route._MIN_CONTENT_LEN)
    got = await route.ingest_url(route.UrlIn(url="https://example.com/a"), user_id="u1")
    assert got == {"chunks": 3, "title": "标题", "source_url": "https://example.com/a"}

    _fake_fetch(monkeypatch, "正" * (route._MIN_CONTENT_LEN - 1))
    with pytest.raises(HTTPException):
        await route.ingest_url(route.UrlIn(url="https://example.com/a"), user_id="u1")

