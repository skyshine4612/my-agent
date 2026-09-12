# 会话文件存储测试：纯文件系统 + tmp_path，不连库（归属靠目录结构，不靠 DB）。
import pytest

from app.core.files import MAX_NAME_CHARS, FileStore


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


async def test_save_sanitizes_filename(store):
    """路径与换行在落盘前就被剥掉：这个名字要进清单给模型看，也是它报回来时的查表键。"""
    saved = await store.save("u1", "c1", "../../etc/passwd", b"x")
    assert saved["name"] == "passwd"
    meta, _ = await store.read("u1", "c1", saved["file_id"])   # 仍落在本会话目录里，没跑到外面
    assert meta["name"] == "passwd"


async def test_save_strips_windows_paths_and_control_chars(store):
    assert (await store.save("u1", "c1", "a\\b.xlsx", b"x"))["name"] == "b.xlsx"
    assert (await store.save("u1", "c1", "evil\nname.xlsx", b"x"))["name"] == "evilname.xlsx"
    assert (await store.save("u1", "c1", "", b"x"))["name"] == "未命名文件"


async def test_save_truncates_long_name_keeping_suffix(store):
    """截断必须保后缀：openpyxl 靠后缀判格式，丢了后缀脚本直接拒绝打开。"""
    saved = await store.save("u1", "c1", "长" * 200 + ".xlsx", b"x")
    assert saved["name"] == "长" * (MAX_NAME_CHARS - 5) + ".xlsx"


async def test_list_by_name_keeps_newest_of_same_name(store):
    """同名取最新：一个名字只能对应一个文件，否则模型看到的名字与打开的文件可能不是同一个。

    list_files 是新→旧，「最新」= 先写进映射的那条，所以必须 setdefault —— 字典推导式是后写覆盖，旧件反而赢。
    """
    await store.save("u1", "c1", "报表.xlsx", b"old-data")
    new = await store.save("u1", "c1", "报表.xlsx", b"new")
    got = await store.list_by_name("u1", "c1")
    assert list(got) == ["报表.xlsx"]
    assert got["报表.xlsx"]["file_id"] == new["file_id"]
    assert got["报表.xlsx"]["size"] == 3          # 不是旧件那 8 字节


async def test_list_by_name_is_scoped_to_owner_and_conversation(store):
    await store.save("u1", "c1", "a.xlsx", b"x")
    assert await store.list_by_name("u1", "c1") != {}
    assert await store.list_by_name("u2", "c1") == {}
    assert await store.list_by_name("u1", "c2") == {}
    assert await store.list_by_name("u1", "nope") == {}
