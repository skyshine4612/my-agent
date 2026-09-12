# tests/test_knowledge_route.py
# 建库路由的失败路径：入库异常必须留日志，且外发的错误文本要按值脱敏。
# 直接测 _ingest helper，不经过 HTTP 层与数据库。
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
