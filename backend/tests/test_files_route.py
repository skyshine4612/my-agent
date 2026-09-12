# 上传/下载路由测试：不连库——会话归属与自动建会话都用替身，文件落在 tmp_path。
import io

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_user
from app.core.files import FileStore
from app.main import app
from app.services.agent_service import service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "file_store", FileStore(tmp_path))

    async def _belongs_to(user_id, conv_id):
        return conv_id == "c1"

    async def _create_conversation(user_id, title=None):
        return "c-new"

    monkeypatch.setattr(service.conversation_store, "belongs_to", _belongs_to)
    monkeypatch.setattr(service.conversation_store, "create_conversation", _create_conversation)
    app.dependency_overrides[get_current_user] = lambda: "u-test"
    yield TestClient(app)
    app.dependency_overrides.clear()


def _upload(client, name, data=b"PK\x03\x04", conv="c1"):
    return client.post("/api/files",
                       files={"file": (name, io.BytesIO(data),
                                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                       data={"conversation_id": conv} if conv else {})


def test_upload_saves_and_returns_ids(client):
    r = _upload(client, "销售表.xlsx")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "销售表.xlsx" and body["size"] == 4 and body["conversation_id"] == "c1"
    assert body["file_id"]


def test_upload_rejects_other_suffixes(client):
    assert _upload(client, "a.txt").status_code == 400


def test_upload_without_conversation_creates_one(client):
    r = _upload(client, "a.xlsx", conv=None)
    assert r.status_code == 200 and r.json()["conversation_id"] == "c-new"


def test_upload_to_someone_elses_conversation_rebuilds(client):
    # 归属校验失败时当作首次对话重建，与 AgentService._ensure_conversation 同语义
    r = _upload(client, "a.xlsx", conv="c-other")
    assert r.status_code == 200 and r.json()["conversation_id"] == "c-new"


def test_download_returns_bytes(client):
    file_id = _upload(client, "销售表.xlsx").json()["file_id"]
    r = client.get(f"/api/files/{file_id}", params={"conversation_id": "c1"})
    assert r.status_code == 200 and r.content == b"PK\x03\x04"
    assert "attachment" in r.headers["content-disposition"]
    # 中文文件名按 RFC 5987 编码，避免非 ASCII 头
    assert "%E9%94%80" in r.headers["content-disposition"]


def test_download_isolated_by_user(client, monkeypatch):
    file_id = _upload(client, "a.xlsx").json()["file_id"]
    app.dependency_overrides[get_current_user] = lambda: "u-other"
    assert client.get(f"/api/files/{file_id}", params={"conversation_id": "c1"}).status_code == 404


def test_download_rejects_illegal_file_id(client):
    assert client.get("/api/files/not-a-uuid", params={"conversation_id": "c1"}).status_code == 404
