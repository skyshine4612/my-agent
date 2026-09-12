# 会话文件存储测试：纯文件系统 + tmp_path，不连库（归属靠目录结构，不靠 DB）。
import pytest

from app.core.files import FileStore


@pytest.fixture
def store(tmp_path):
    return FileStore(tmp_path)


async def test_save_then_read_roundtrip(store):
    saved = await store.save("u1", "c1", "销售表.xlsx", b"data", source="upload")
    got = await store.read("u1", "c1", saved["file_id"])
    assert got is not None
    meta, data = got
    assert data == b"data"
    assert meta["name"] == "销售表.xlsx"
    assert meta["source"] == "upload"
    assert meta["size"] == 4


async def test_read_rejects_illegal_file_id(store):
    assert await store.read("u1", "c1", "../../etc") is None
    assert await store.read("u1", "c1", "not-a-uuid") is None


async def test_read_is_scoped_to_owner_and_conversation(store):
    saved = await store.save("u1", "c1", "a.xlsx", b"x")
    assert await store.read("u2", "c1", saved["file_id"]) is None
    assert await store.read("u1", "c2", saved["file_id"]) is None


async def test_list_files_returns_saved_files(store):
    a = await store.save("u1", "c1", "a.xlsx", b"a")
    b = await store.save("u1", "c1", "b.xlsx", b"b")
    files = await store.list_files("u1", "c1", limit=10)
    assert {f["file_id"] for f in files} == {a["file_id"], b["file_id"]}
    assert len(await store.list_files("u1", "c1", limit=1)) == 1


async def test_list_files_empty_for_unknown_conversation(store):
    assert await store.list_files("u1", "nope") == []


async def test_delete_conversation_keeps_siblings(store):
    await store.save("u1", "c1", "a.xlsx", b"a")
    keep = await store.save("u1", "c2", "b.xlsx", b"b")
    await store.delete_conversation("u1", "c1")
    assert await store.list_files("u1", "c1") == []
    assert len(await store.list_files("u1", "c2")) == 1
    assert (await store.read("u1", "c2", keep["file_id"])) is not None
