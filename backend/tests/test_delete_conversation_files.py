# 删会话要清两棵树：result_dir（spill）与 file_dir（上传件 + 产物）。
# 只清一棵的话，另一棵会随会话数增长无限堆积，且没有任何报错。
from app.core.files import FileStore
from app.services.agent_service import service


async def test_delete_conversation_clears_file_tree(monkeypatch, tmp_path):
    store = FileStore(tmp_path)
    await store.save("u1", "c1", "a.xlsx", b"x")
    monkeypatch.setattr(service, "file_store", store)

    async def _ok(user_id, conv_id):
        return True

    monkeypatch.setattr(service.memory, "delete_conversation", _ok)

    assert await service.delete_conversation("u1", "c1") is True
    assert await store.list_files("u1", "c1") == []


async def test_failed_ownership_check_keeps_files(monkeypatch, tmp_path):
    store = FileStore(tmp_path)
    await store.save("u1", "c1", "a.xlsx", b"x")
    monkeypatch.setattr(service, "file_store", store)

    async def _denied(user_id, conv_id):
        return False

    monkeypatch.setattr(service.memory, "delete_conversation", _denied)

    assert await service.delete_conversation("u2", "c1") is False
    assert len(await store.list_files("u1", "c1")) == 1
