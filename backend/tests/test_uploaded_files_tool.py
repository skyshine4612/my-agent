# list_uploaded_files 工具测试：真 FileStore（假 Storage、真库）+ contextvar 注入 user/conversation。
# 这份清单以前每轮写进 system prompt，改为按需查后，隔离与来源标注这两件事失去了 system 侧的兜底，
# 只能在工具里钉住：越权会把别人的文件名报出来，来源标注丢了模型就分不清哪个是自己刚生成的。
import pytest_asyncio

from app.core.agent import NON_QUERY_TOOLS
from app.core.context import current_conversation_id, current_user_id
from app.core.files import FileStore
from app.core.memory import ConversationStore
from app.tools.file import LIST_LIMIT, make_list_uploaded_files
from tests.fake_storage import make_client


@pytest_asyncio.fixture
async def env(db, user_id, new_conversation):
    """(store, uid, cid)：uid / cid 是库里真实的行——files 表的两个外键要求如此。"""
    storage, _ = make_client()
    cid = await new_conversation()
    current_user_id.set(user_id)
    current_conversation_id.set(cid)
    return FileStore(storage), user_id, cid


async def test_lists_upload_and_marks_artifacts(env):
    store, uid, cid = env
    await store.save(uid, cid, "销售表.xlsx", b"x" * 2411724)
    await store.save(uid, cid, "已脱敏.xlsx", b"y", source="script:excel/desensitize")
    out = await make_list_uploaded_files(store).ainvoke({})
    assert "- 销售表.xlsx  2.3MB" in out
    assert "由 excel/desensitize 生成" in out


async def test_empty_conversation_falls_back_to_text(env):
    """空清单必须给一句话：返回空串时模型会以为工具坏了，转而凭印象编文件名。"""
    store, _, _ = env
    out = await make_list_uploaded_files(store).ainvoke({})
    assert out == "本会话还没有任何文件。"


async def test_scoped_to_current_user_and_conversation(env, new_conversation, make_user):
    store, uid, cid = env
    other_conv = await new_conversation()                     # 同一用户的另一个会话
    other_user = await make_user()
    other_users_conv = await ConversationStore().create_conversation(other_user)
    await store.save(uid, cid, "本会话.xlsx", b"x")
    await store.save(uid, other_conv, "别的会话.xlsx", b"x")
    await store.save(other_user, other_users_conv, "别人的.xlsx", b"x")
    out = await make_list_uploaded_files(store).ainvoke({})
    assert "本会话.xlsx" in out
    assert "别的会话.xlsx" not in out and "别人的.xlsx" not in out


async def test_tool_is_declared_non_query():
    """漏登记不会报错，只会让清单被结构化截断成前几个文件，后面的变成「查不到」，故由测试钉住。"""
    assert "list_uploaded_files" in NON_QUERY_TOOLS


def test_list_limit_fits_within_file_ref_limit():
    """清单 limit 必须 ≤ 反查 limit，否则出现「清单里看得见、引用时解析不到」的死角。"""
    from app.core.files import FILE_REF_LIMIT

    assert LIST_LIMIT <= FILE_REF_LIMIT
