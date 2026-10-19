# AgentService.delete_conversation 的接线测试：与 test_files.py 分工不同——
# 那边测 FileStore 自己，这边测 service 层的「先删对象、再删会话行」顺序与归属校验语义。
from app.core.storage.files import FileStore
from app.core.memory import ConversationStore
from app.services.agent_service import service
from tests.fake_storage import make_client


async def test_deletes_storage_objects_before_the_conversation_row(db, user_id, new_conversation,
                                                                  monkeypatch):
    """顺序写死：先删对象、再删会话行。

    反过来（先删行）的话，conversations 级联会先把 files 行删光——今天前缀删不读表，
    看不出区别，但任何一次「读表再删」的改动都会静默失效。所以钉在「删行那一刻对象必须已经没了」。
    """
    storage, fake = make_client()
    store = FileStore(storage)
    conv = await new_conversation()
    await store.save(user_id, conv, "a.xlsx", b"x")
    monkeypatch.setattr(service, "file_store", store)

    at_row_delete = {}

    async def _delete_row(uid, cid):
        at_row_delete["objects"] = dict(fake.objects)
        return await ConversationStore().delete_conversation(uid, cid)

    monkeypatch.setattr(service.memory, "delete_conversation", _delete_row)

    assert await service.delete_conversation(user_id, conv) is True
    assert at_row_delete["objects"] == {}                 # 删行时对象已清干净
    assert await store.list_files(user_id, conv) == []    # 行靠 conversations 级联走


async def test_failed_ownership_check_keeps_files(db, user_id, new_conversation, make_user,
                                                  monkeypatch):
    """非本人调用：前缀里带的是请求者自己的 user_id，匹配不到任何对象，是空操作。"""
    storage, fake = make_client()
    store = FileStore(storage)
    conv = await new_conversation()
    await store.save(user_id, conv, "a.xlsx", b"x")
    monkeypatch.setattr(service, "file_store", store)

    async def _denied(uid, cid):
        return False

    monkeypatch.setattr(service.memory, "delete_conversation", _denied)

    assert await service.delete_conversation(await make_user(), conv) is False
    assert len(await store.list_files(user_id, conv)) == 1
    assert fake.objects != {}
