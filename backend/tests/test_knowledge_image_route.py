# 图片代理端点：带登录、路径形状受限、source_key 必须命中已入库文档。
# 走 TestClient 会绕开真实鉴权，所以直接调路由函数并注入 user_id。
import pytest
from fastapi import HTTPException

from app.api.routes.knowledge import get_knowledge_image
from app.core.storage import BUCKET_KNOWLEDGE
from app.services.agent_service import service
from tests.fake_storage import make_client


@pytest.fixture
def png_in_store(monkeypatch):
    """把一张 png 塞进 service.storage 背后的假后端。"""
    client, fake = make_client()
    monkeypatch.setattr(service, "storage", client)
    key = "a" * 16 + "/" + "b" * 32 + "/0.png"
    fake.objects[(BUCKET_KNOWLEDGE, key)] = b"PNGDATA"
    return key


async def test_returns_bytes_with_nosniff(png_in_store, user_id, monkeypatch):
    async def has(_u, _k):
        return True

    monkeypatch.setattr(service.knowledge, "has_source_key", has)
    resp = await get_knowledge_image("a" * 16, "b" * 32, "0.png", user_id=user_id)
    assert resp.body == b"PNGDATA"
    assert resp.media_type == "image/png"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_rejects_unknown_source_key(png_in_store, user_id, monkeypatch):
    async def has(_u, _k):
        return False

    monkeypatch.setattr(service.knowledge, "has_source_key", has)
    with pytest.raises(HTTPException) as e:
        await get_knowledge_image("a" * 16, "b" * 32, "0.png", user_id=user_id)
    assert e.value.status_code == 404


async def test_rejects_svg_and_bad_shapes(user_id):
    """svg 不回吐：一个含 <script> 的 svg 以内联方式送回应用同源，
    <img> 里不执行，但「点击放大」的直接导航会执行。"""
    for args in [("a" * 16, "b" * 32, "0.svg"),
                 ("not-hex", "b" * 32, "0.png"),
                 ("a" * 16, "short", "0.png"),
                 ("a" * 16, "b" * 32, "../../etc/passwd")]:
        with pytest.raises(HTTPException) as e:
            await get_knowledge_image(*args, user_id=user_id)
        assert e.value.status_code == 404


async def test_missing_object_is_404(png_in_store, user_id, monkeypatch):
    async def has(_u, _k):
        return True

    monkeypatch.setattr(service.knowledge, "has_source_key", has)
    with pytest.raises(HTTPException) as e:
        await get_knowledge_image("a" * 16, "b" * 32, "7.png", user_id=user_id)
    assert e.value.status_code == 404
