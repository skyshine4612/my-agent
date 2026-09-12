# 会话文件存储测试：真 Postgres（files 表 + RLS）+ 内存假 Storage。
# 不再用 tmp_path：归属判定从「目录结构」搬到了「行里的 user_id / conversation_id」，
# 不连库就测不到隔离这一层。
import pytest

from app.core.files import MAX_NAME_CHARS, FileStore
from app.core.memory import ConversationStore
from app.core.storage import BUCKET_FILES
from tests.fake_storage import make_client


@pytest.fixture
def store():
    client, fake = make_client()
    return FileStore(client), fake


async def test_save_then_read_roundtrip(store, user_id, new_conversation):
    s, fake = store
    conv = await new_conversation()
    saved = await s.save(user_id, conv, "销售表.xlsx", b"data", source="upload")
    got = await s.read(user_id, conv, saved["file_id"])
    assert got is not None
    meta, data = got
    assert data == b"data"
    assert meta["name"] == "销售表.xlsx"
    assert meta["source"] == "upload"
    assert meta["size"] == 4
    # 字节真的进了 Storage，不是又落回本地磁盘
    assert fake.objects[("files", f"{user_id}/{conv}/{saved['file_id']}/original.xlsx")] == b"data"


async def test_read_returns_none_for_missing_object(store, user_id, new_conversation):
    """行在、对象没了 → None。下载路由按「不区分 404 原因」处理。"""
    s, fake = store
    conv = await new_conversation()
    saved = await s.save(user_id, conv, "a.xlsx", b"x")
    fake.objects.clear()
    assert await s.read(user_id, conv, saved["file_id"]) is None


async def test_read_rejects_illegal_file_id(store, user_id, new_conversation):
    s, _ = store
    conv = await new_conversation()
    assert await s.read(user_id, conv, "../../etc") is None
    assert await s.read(user_id, conv, "not-a-uuid") is None


async def test_read_is_scoped_to_owner_and_conversation(store, user_id, make_user, new_conversation):
    s, _ = store
    conv = await new_conversation()
    other_user = await make_user()
    saved = await s.save(user_id, conv, "a.xlsx", b"x")
    assert await s.read(other_user, conv, saved["file_id"]) is None
    assert await s.read(user_id, await new_conversation(), saved["file_id"]) is None


async def test_save_compensates_when_insert_fails(store, user_id):
    """insert 失败要补偿删对象：留下的是「无人引用的字节」，不是「看得见读不到的悬空行」。"""
    s, fake = store
    # conversation_id 不存在 → 外键失败
    with pytest.raises(Exception):
        await s.save(user_id, "没有这个会话", "a.xlsx", b"x")
    assert fake.objects == {}


async def test_list_files_is_newest_first(store, user_id, new_conversation):
    s, _ = store
    conv = await new_conversation()
    a = await s.save(user_id, conv, "a.xlsx", b"a")
    b = await s.save(user_id, conv, "b.xlsx", b"b")
    files = await s.list_files(user_id, conv, limit=10)
    assert [f["file_id"] for f in files] == [b["file_id"], a["file_id"]]
    assert len(await s.list_files(user_id, conv, limit=1)) == 1
    assert await s.list_files(user_id, conv, limit=1) == files[:1]


async def test_list_files_empty_for_unknown_conversation(store, user_id):
    s, _ = store
    assert await s.list_files(user_id, "nope") == []


async def test_delete_conversation_keeps_siblings(store, user_id, new_conversation):
    """只管字节：行不归这里删（见下一条用例），所以本用例不对行做断言。"""
    s, fake = store
    c1, c2 = await new_conversation(), await new_conversation()
    await s.save(user_id, c1, "a.xlsx", b"a")
    keep = await s.save(user_id, c2, "b.xlsx", b"b")
    await s.delete_conversation(user_id, c1)
    assert set(fake.objects) == {("files", f"{user_id}/{c2}/{keep['file_id']}/original.xlsx")}


async def test_conversation_cascade_removes_rows(user_id, new_conversation, store):
    """行归 conversations 级联删（files.conversation_id 的 on delete cascade）。

    实现改成不读表的删法后，「元数据怎么没的」就全压在这条外键上：
    漏了级联就是一批永不被回收的行，且 list_files 会一直把它们列出来。
    """
    s, _ = store
    conv = await new_conversation()
    await s.save(user_id, conv, "a.xlsx", b"a")
    await ConversationStore().delete_conversation(user_id, conv)
    assert await s.list_files(user_id, conv) == []


async def test_delete_conversation_swallows_storage_failure(store, user_id, new_conversation):
    """清理失败只记日志、照常返回：会话已经删了，这里抛异常会让路由 500，
    客户端重试时 belongs_to 为假 → 前端永远显示「删除失败」。"""
    s, _ = store
    conv = await new_conversation()
    await s.save(user_id, conv, "a.xlsx", b"a")

    async def boom(*a, **kw):
        raise RuntimeError("storage down")

    s.storage.delete_prefix = boom
    await s.delete_conversation(user_id, conv)      # 不抛


async def test_save_sanitizes_filename(store, user_id, new_conversation):
    """路径与换行在落盘前就被剥掉：这个名字要进清单给模型看，也是它报回来时的查表键。"""
    s, fake = store
    conv = await new_conversation()
    saved = await s.save(user_id, conv, "../../etc/passwd", b"x")
    assert saved["name"] == "passwd"
    meta, _ = await s.read(user_id, conv, saved["file_id"])
    assert meta["name"] == "passwd"
    assert ("files", f"{user_id}/{conv}/{saved['file_id']}/original") in fake.objects


async def test_save_strips_windows_paths_and_control_chars(store, user_id, new_conversation):
    s, _ = store
    conv = await new_conversation()
    assert (await s.save(user_id, conv, "a\\b.xlsx", b"x"))["name"] == "b.xlsx"
    assert (await s.save(user_id, conv, "evil\nname.xlsx", b"x"))["name"] == "evilname.xlsx"
    assert (await s.save(user_id, conv, "", b"x"))["name"] == "未命名文件"


async def test_save_truncates_long_name_keeping_suffix(store, user_id, new_conversation):
    """截断必须保后缀：openpyxl 靠后缀判格式，丢了后缀脚本直接拒绝打开。"""
    s, _ = store
    conv = await new_conversation()
    saved = await s.save(user_id, conv, "长" * 200 + ".xlsx", b"x")
    assert saved["name"] == "长" * (MAX_NAME_CHARS - 5) + ".xlsx"


async def test_object_key_is_ascii_only(store, user_id, new_conversation):
    """对象 key 必须全 ASCII：storage-api 对含非 ASCII 的 key 直接 400 InvalidKey。

    脚本产物名不受 .xlsx 限制，`报表.表格` 这种扩展名判不进白名单就丢掉——
    扩展名只是给 key 一点可读性，真名在 `files.name`、`Content-Disposition` 用它，丢它不影响下载。
    """
    s, fake = store
    conv = await new_conversation()
    saved = await s.save(user_id, conv, "报表.表格", b"x", source="script:x/y")
    assert saved["name"] == "报表.表格"
    key = f"{user_id}/{conv}/{saved['file_id']}/original"
    assert (BUCKET_FILES, key) in fake.objects
    assert key.isascii()
    assert await s.read(user_id, conv, saved["file_id"]) is not None


async def test_list_by_name_keeps_newest_of_same_name(store, user_id, new_conversation):
    """同名取最新：一个名字只能对应一个文件，否则模型看到的名字与打开的文件可能不是同一个。"""
    s, _ = store
    conv = await new_conversation()
    await s.save(user_id, conv, "报表.xlsx", b"old-data")
    new = await s.save(user_id, conv, "报表.xlsx", b"new")
    got = await s.list_by_name(user_id, conv)
    assert list(got) == ["报表.xlsx"]
    assert got["报表.xlsx"]["file_id"] == new["file_id"]
    assert got["报表.xlsx"]["size"] == 3          # 不是旧件那 8 字节


async def test_list_by_name_applies_limit_before_dedup(store, user_id, new_conversation):
    """limit 必须施加在子查询里：挪到外层，「参与去重的集合」就从「最近 N 个文件」
    变成「字母序前 N 个名字」。"""
    s, _ = store
    conv = await new_conversation()
    await s.save(user_id, conv, "a.xlsx", b"a")
    await s.save(user_id, conv, "b.xlsx", b"b")
    assert set(await s.list_by_name(user_id, conv, limit=1)) == {"b.xlsx"}


async def test_list_by_name_is_scoped_to_owner_and_conversation(store, user_id, make_user,
                                                               new_conversation):
    s, _ = store
    conv = await new_conversation()
    await s.save(user_id, conv, "a.xlsx", b"x")
    other_user = await make_user()
    assert await s.list_by_name(user_id, conv) != {}
    assert await s.list_by_name(other_user, conv) == {}
    assert await s.list_by_name(user_id, await new_conversation()) == {}
    assert await s.list_by_name(user_id, "nope") == {}
