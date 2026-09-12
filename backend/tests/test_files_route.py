# 上传/下载路由测试：真 Postgres + 内存假 Storage。
# 元数据搬进 files 表后，user_id 与 conversation_id 都成了真外键，
# 再像以前那样拿 "u-test" / "c1" 这种字面量顶替会直接撞外键。
#
# **不能用 TestClient**：它的 ASGI portal 跑在另一个 event loop 上，而 db fixture 的连接池
# 绑在测试自己的 loop 上，跨 loop 复用 asyncpg 连接会 `InterfaceError`。改用同 loop 的 ASGITransport。
import httpx
import pytest

from app.core.auth import get_current_user
from app.core.files import FileStore
from app.core.memory import ConversationStore
from app.main import app
from app.services.agent_service import service
from tests.fake_storage import make_client

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture
async def client(db, user_id, monkeypatch):
    """登录身份＝user_id，文件存储换成接在假 Storage 上的真 FileStore（落库照常走 rls_conn）。"""
    storage, _ = make_client()
    monkeypatch.setattr(service, "file_store", FileStore(storage))
    app.dependency_overrides[get_current_user] = lambda: user_id
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _upload(c, name, data=b"PK\x03\x04", conv=None):
    return await c.post("/api/files", files={"file": (name, data, XLSX_MIME)},
                        data={"conversation_id": conv} if conv else None)


async def test_upload_saves_and_returns_ids(client, new_conversation):
    conv = await new_conversation()
    r = await _upload(client, "销售表.xlsx", conv=conv)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "销售表.xlsx" and body["size"] == 4 and body["conversation_id"] == conv
    assert body["file_id"]


async def test_upload_rejects_other_suffixes(client, new_conversation):
    conv = await new_conversation()
    assert (await _upload(client, "a.txt", conv=conv)).status_code == 400


async def test_upload_without_conversation_creates_one(client):
    first = (await _upload(client, "a.xlsx")).json()["conversation_id"]
    assert first
    # 带上这个新 id 再传一次：belongs_to 认它 → 原样返回，说明建的是真会话而不是随手编的串
    assert (await _upload(client, "b.xlsx", conv=first)).json()["conversation_id"] == first


async def test_upload_to_someone_elses_conversation_rebuilds(client, make_user):
    """他人的会话 id 当作没有，重建自己的，杜绝跨用户写入。"""
    other = await make_user()
    theirs = await ConversationStore().create_conversation(other)
    got = (await _upload(client, "a.xlsx", conv=theirs)).json()["conversation_id"]
    assert got != theirs


async def test_download_returns_bytes(client, new_conversation):
    conv = await new_conversation()
    file_id = (await _upload(client, "销售表.xlsx", conv=conv)).json()["file_id"]
    r = await client.get(f"/api/files/{file_id}", params={"conversation_id": conv})
    assert r.status_code == 200 and r.content == b"PK\x03\x04"
    assert "attachment" in r.headers["content-disposition"]
    # 中文文件名按 RFC 5987 编码，避免非 ASCII 头
    assert "%E9%94%80" in r.headers["content-disposition"]


async def test_download_isolated_by_user(client, new_conversation, make_user):
    conv = await new_conversation()
    file_id = (await _upload(client, "a.xlsx", conv=conv)).json()["file_id"]
    other = await make_user()
    app.dependency_overrides[get_current_user] = lambda: other
    r = await client.get(f"/api/files/{file_id}", params={"conversation_id": conv})
    assert r.status_code == 404


async def test_download_rejects_illegal_file_id(client, new_conversation):
    conv = await new_conversation()
    r = await client.get("/api/files/not-a-uuid", params={"conversation_id": conv})
    assert r.status_code == 404
