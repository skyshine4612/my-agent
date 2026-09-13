# tests/test_knowledge.py
# 知识库集成测试：切块入库 + 语义检索 + 跨用户共享 + 原图进 Storage，走真实 Postgres 的 documents 表。
# 向量由 FakeEmbedder 给（见 conftest），绕开真实 embedding API；排序可预期，真实语义质量由端到端验证。
# 图片字节进内存假 Storage（tests/fake_storage.py），元数据照常落真库。
import hashlib

import pytest

from app.core.db import rls_conn
from app.core.knowledge import SIMILARITY_FLOOR, KnowledgeStore, filter_relevant
from app.core.storage import BUCKET_KNOWLEDGE
from tests.fake_storage import make_client

# 段落各自成块的配置：短段落会被合并，想验「一块一段」的用例就把 chunk_size 压到段落长度量级
_PER_PARA = {"chunk_size": 12, "overlap": 0}


def _source_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _img(n=0, desc="西湖断桥"):
    return {"n": n, "desc": desc, "data": b"PNGDATA", "mime": "image/png"}


def _text(head: str) -> str:
    """带 IMG0 占位符的正文。图片靠占位符的**偏移**才能挂上块，
    所以凡是要验「图进了桶」或「旧目录被回收」的用例，正文里必须有它 ——
    没有占位符时 assign_images 一张都挂不上，断言会变成对空集的空转。"""
    return f"\x00IMG0\x00{head}" + "甲。" * 300


@pytest.fixture
def kb(fake_embedder):
    """造 store 的工厂：(store, fake)。每个 store 配一个独立的内存假 Storage，
    而元数据一律落真库——图片归属、替换、删除的断言都要靠真表。"""
    def _make(**kw):
        client, fake = make_client()
        return KnowledgeStore(client, embedder=fake_embedder, **kw), fake
    return _make


async def test_ingest_chunks_and_lists(kb, user_id):
    """多段文本按段落切块入库，list_documents 能按来源聚合出该文档。"""
    store, _ = kb(**_PER_PARA)
    n = await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。\n\n长城很长。",
                           title="杭州攻略", source_url="http://example.com/hz")
    assert n == 3

    docs = await store.list_documents(user_id)
    assert len(docs) == 1
    assert docs[0]["title"] == "杭州攻略"
    assert docs[0]["chunks"] == 3


async def test_short_paragraphs_land_in_one_chunk(kb, user_id):
    """默认配置下短段落合并成一个片段：块数不随段落数线性上涨。"""
    store, _ = kb()
    n = await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。\n\n长城很长。", title="攻略")
    assert n == 1


async def test_ingest_empty_text_writes_nothing(kb, user_id):
    """纯空白文本切不出块，不写库也不报错。"""
    store, _ = kb()
    assert await store.ingest(user_id, "   \n\n  ", title="空") == 0
    assert await store.list_documents(user_id) == []


async def test_search_ranks_by_similarity(kb, user_id):
    """检索按余弦相似度降序，且 top_k 限制返回条数。"""
    store, _ = kb(**_PER_PARA)
    await store.ingest(user_id, "西湖值得去。\n\n西湖冬天最美。\n\n火锅值得吃。", title="攻略")

    hits = await store.search(user_id, "西湖怎么玩", top_k=3)
    # 命中两块西湖；火锅那块与问题无关（假 embedder 下相似度 0），被相关性下限挡掉
    assert len(hits) == 2
    assert all("西湖" in h["content"] for h in hits)
    # top_k 生效：库里有 2 块西湖，要 1 条就只给 1 条
    assert len(await store.search(user_id, "西湖", top_k=1)) == 1


async def test_search_returns_nothing_when_nothing_is_relevant(kb, user_id):
    """库里只有无关内容时返回空——这是 search_knowledge 回「知识库暂无相关内容」的唯一触发点。

    没有这道门槛的话，match_documents 是纯 top-k、不问相关性，库里只要有数据就一定返回：
    问苏州、库里全是成都，也会把成都的片段当「最相关」交给模型（见 SIMILARITY_FLOOR）。
    """
    store, _ = kb(**_PER_PARA)
    await store.ingest(user_id, "火锅值得吃。", title="攻略")

    assert await store.search(user_id, "西湖怎么玩", top_k=3) == []


def test_filter_relevant_pins_the_floor():
    """相关性下限的纯函数契约：低于门槛丢掉、缺 similarity 的按无关丢掉，不起库。"""
    hits = [{"content": "相关", "similarity": 0.7},
            {"content": "擦边", "similarity": SIMILARITY_FLOOR},
            {"content": "无关", "similarity": 0.3},
            {"content": "缺字段"}]
    assert [h["content"] for h in filter_relevant(hits)] == ["相关", "擦边"]


async def test_search_empty_library(kb, user_id):
    """空库检索返回空列表，不报错。"""
    store, _ = kb()
    assert await store.search(user_id, "西湖", top_k=3) == []


async def test_knowledge_is_shared_across_users(kb, user_id, make_user):
    """知识库是全局共享的：A 入库的内容，B 也能检索到（策略是 using(true)，不按 user 隔离）。"""
    store, _ = kb()
    other = await make_user()
    await store.ingest(user_id, "西湖的断桥在冬天最美。", title="杭州攻略")

    hits = await store.search(other, "西湖", top_k=3)
    assert len(hits) == 1
    assert hits[0]["title"] == "杭州攻略"


async def test_delete_by_source_removes_all_chunks(kb, user_id):
    """按来源删除会清掉该文档的所有块，不留残片。"""
    store, _ = kb(**_PER_PARA)
    await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。", source_url="http://example.com/a")
    await store.ingest(user_id, "长城很长。", source_url="http://example.com/b")

    assert await store.delete_by_source(user_id, "http://example.com/a") == 2
    docs = await store.list_documents(user_id)
    assert len(docs) == 1
    assert docs[0]["source_url"] == "http://example.com/b"
    assert docs[0]["chunks"] == 1


async def test_ingest_uploads_images_and_links_them_to_a_chunk(kb, user_id):
    store, fake = kb()
    text = "甲。\x00IMG0\x00\n\n" + "乙。" * 400
    await store.ingest(user_id, text, title="杭州", source_url="http://a/1",
                       images=[_img()])
    bucket, key = next(iter(fake.objects))
    assert bucket == BUCKET_KNOWLEDGE
    assert key.startswith(_source_key("http://a/1") + "/")
    assert key.endswith("/0.png")
    assert fake.objects[(bucket, key)] == b"PNGDATA"

    async with rls_conn(user_id) as conn:
        rows = await conn.fetch("select content, images from documents order by chunk_index")
    assert rows[0]["images"] == [{"key": key, "desc": "西湖断桥"}]
    assert "【图片：西湖断桥】" in rows[0]["content"]
    # 占位符与 \x00 一个都不许留
    assert all("\x00" not in r["content"] for r in rows)
    assert all("IMG" not in r["content"] for r in rows)


async def test_ingest_without_source_url_skips_images(kb, user_id):
    """source_url 可空：图片目录的第一段就是它的哈希，没有它就没有归属，
    硬编一个反而留下永远回收不了的对象。"""
    store, fake = kb()
    await store.ingest(user_id, "甲。\x00IMG0\x00", title="t", images=[_img()])
    assert fake.objects == {}
    async with rls_conn(user_id) as conn:
        rows = await conn.fetch("select content, images from documents")
    assert rows[0]["images"] == []


async def test_ingest_image_upload_failure_only_loses_the_image(kb, user_id, monkeypatch):
    """降级分开对待：爬虫图是增强项，因一张图把整次抓取搞成 502 远不划算。"""
    store, _ = kb()

    async def boom(*a, **kw):
        raise RuntimeError("storage down")

    monkeypatch.setattr(store.storage, "put", boom)
    n = await store.ingest(user_id, "甲。\x00IMG0\x00", title="t",
                           source_url="http://a/1", images=[_img()])
    assert n == 1
    async with rls_conn(user_id) as conn:
        rows = await conn.fetch("select content, images from documents")
    assert rows[0]["images"] == [{"key": None, "desc": "西湖断桥"}]
    assert "【图片：西湖断桥】" in rows[0]["content"]


async def test_reingest_replaces_instead_of_stacking(kb, user_id):
    """重抓同一 URL 是替换不是叠加：今天的行为是叠加，检索会同时召回两版。"""
    store, fake = kb()
    n_old = await store.ingest(user_id, _text("旧正文"), title="t", source_url="http://a/1",
                               images=[_img(desc="旧图")])
    old_keys = set(fake.objects)
    assert old_keys                                          # 先有旧目录，下面那句才不是空转
    n_new = await store.ingest(user_id, _text("新正文"), title="t", source_url="http://a/1",
                               images=[_img(desc="新图")])
    async with rls_conn(user_id) as conn:
        rows = await conn.fetch("select content from documents")
    # 剩下的正好是新版自己的块数，不是 n_old + n_new —— 用同一篇的块数当尺子，
    # 免得把「这篇本来就切两块」误当成叠加
    assert len(rows) == n_new
    assert all("旧正文" not in r["content"] for r in rows)
    assert set(fake.objects).isdisjoint(old_keys)            # 旧目录已回收


async def test_reingest_never_deletes_another_ingest_directory(kb, user_id):
    """第 4 步只删 delete ... returning 捕获到的那批 ingest_id ——
    写成「删 <source_key>/ 下除我之外的全部」会误删并发重抓的对方目录。"""
    store, fake = kb()
    await store.ingest(user_id, _text("甲"), title="t", source_url="http://a/1", images=[_img()])
    other = (BUCKET_KNOWLEDGE, _source_key("http://a/1") + "/" + "f" * 32 + "/0.png")
    fake.objects[other] = b"somebody-else"
    await store.ingest(user_id, _text("乙"), title="t", source_url="http://a/1")
    assert fake.objects.get(other) == b"somebody-else"


async def test_delete_by_source_removes_objects_before_rows(kb, user_id):
    """顺序写死：反过来的话行先没了，前缀删一旦失败就是「永远查不到的孤儿图」。"""
    store, fake = kb()
    await store.ingest(user_id, _text("甲"), title="t", source_url="http://a/1", images=[_img()])
    assert fake.objects
    assert await store.delete_by_source(user_id, "http://a/1") > 0
    assert fake.objects == {}


async def test_delete_by_source_propagates_storage_failure(kb, user_id, monkeypatch):
    """对象删失败**不吞异常**：图还在桶里而界面说删了是更坏的谎。"""
    store, _ = kb()
    await store.ingest(user_id, "甲。" * 300, title="t", source_url="http://a/1")

    async def boom(*a, **kw):
        raise RuntimeError("storage down")

    monkeypatch.setattr(store.storage, "delete_prefix", boom)
    with pytest.raises(RuntimeError):
        await store.delete_by_source(user_id, "http://a/1")


async def test_has_source_key_recognises_an_ingested_url(kb, user_id):
    store, _ = kb()
    await store.ingest(user_id, "甲。" * 300, title="t", source_url="http://a/1")
    assert await store.has_source_key(user_id, _source_key("http://a/1")) is True
    assert await store.has_source_key(user_id, "0" * 16) is False


async def test_search_knowledge_tool_returns_hits_and_images(kb, user_id):
    """工具层把图片从 hits 里提出来单列一份：模型不负责拼图片 URL，
    它由 agent 侧的侧信道原样带给前端（照 artifacts 的路子）。"""
    from app.core.context import current_user_id
    from app.tools.knowledge import make_search_knowledge

    store, _ = kb()
    await store.ingest(user_id, _text("西湖很美，适合散步。"), title="杭州",
                       source_url="http://a/1", images=[_img()])
    # 直接 set：ContextVar 的属性是只读的，monkeypatch.setattr 装不上
    current_user_id.set(user_id)
    got = await make_search_knowledge(store).ainvoke({"query": "西湖"})
    assert isinstance(got, dict)
    assert got["hits"] and "content" in got["hits"][0]
    assert len(got["images"]) == 1
    assert got["images"][0]["desc"] == "西湖断桥"
    assert got["images"][0]["key"]                    # 真的带上了一个对象 key
    # key / desc / ref 三个键：图片字节留在 upload 那一步，不进 hit、更不进模型语料
    assert set(got["images"][0]) == {"key", "desc", "ref"}
    # 正文里的图被编了号，模型照这个号引用（号 = chunk 行 id - 块内下标）
    assert f"【图{got['hits'][0]['id']}-1：西湖断桥】" in got["hits"][0]["content"]


async def test_search_knowledge_tool_keeps_empty_branch(kb, user_id):
    """空分支保留中文短句：改成 dict 后模型会看到 {"hits": [], "images": []}，显式提示就没了。"""
    from app.core.context import current_user_id
    from app.tools.knowledge import make_search_knowledge

    store, _ = kb()
    current_user_id.set(user_id)
    got = await make_search_knowledge(store).ainvoke({"query": "西湖"})
    assert got == "知识库暂无相关内容"


async def test_search_attaches_a_list_of_images(kb, user_id):
    """jsonb codec 不在的话这里是 str，下游全落空 —— 断言形态不只会断言内容。"""
    store, _ = kb()
    await store.ingest(user_id, _text("西湖很美，适合散步。"), title="杭州",
                       source_url="http://a/1", images=[_img()])
    hits = await store.search(user_id, "西湖")
    assert hits and isinstance(hits[0]["images"], list)
    assert hits[0]["images"][0]["desc"] == "西湖断桥"


# ---- number_hit_images：给图编号并把编号写进正文 ----
# 号是「chunk 行 id - 图在块内的下标(从 1 起)」。这个号是**持久**的：同一个 chunk 的
# 同一张图永远得到同一个号，所以模型抄了历史回答里的号也不会指到另一张图上。
# 换成「每轮从 1 开始」的临时编号，test_number_hit_images_ref_is_stable_across_calls 就会红。


def _hit(chunk_id, content, images):
    return {"id": chunk_id, "content": content, "images": images}


def _numbered(hits):
    from app.tools.knowledge import number_hit_images
    return number_hit_images(hits)


def test_number_hit_images_rewrites_marker_and_returns_ref():
    hits = [_hit(13, "看这个。\n【图片：熊猫塔】\n", [{"key": "a/6.jpg", "desc": "熊猫塔"}])]
    images = _numbered(hits)
    assert "【图13-1：熊猫塔】" in hits[0]["content"]
    assert images == [{"key": "a/6.jpg", "desc": "熊猫塔", "ref": "13-1"}]


def test_number_hit_images_numbers_within_chunk_in_order():
    hits = [_hit(7, "【图片：甲】【图片：乙】",
                 [{"key": "a/1.jpg", "desc": "甲"}, {"key": "a/2.jpg", "desc": "乙"}])]
    images = _numbered(hits)
    assert hits[0]["content"] == "【图7-1：甲】【图7-2：乙】"
    assert [i["ref"] for i in images] == ["7-1", "7-2"]


def test_number_hit_images_pairs_identical_descs_by_position():
    """两张图 desc 相同时，str.replace 会把两个 marker 都写成第一个图的号；
    游标 + find 按位置配对才不会错配。"""
    hits = [_hit(5, "【图片：同一张】【图片：同一张】",
                 [{"key": "a/1.jpg", "desc": "同一张"}, {"key": "a/2.jpg", "desc": "同一张"}])]
    images = _numbered(hits)
    assert hits[0]["content"] == "【图5-1：同一张】【图5-2：同一张】"
    assert [i["ref"] for i in images] == ["5-1", "5-2"]
    assert [i["key"] for i in images] == ["a/1.jpg", "a/2.jpg"]


def test_number_hit_images_skips_marker_that_is_gone():
    """占位符被字符硬切切开时，images 列有条目而正文没有 marker：跳过，不瞎挂。"""
    hits = [_hit(3, "正文里没有标记", [{"key": "a/1.jpg", "desc": "孤儿"}])]
    images = _numbered(hits)
    assert hits[0]["content"] == "正文里没有标记"
    assert images == [{"key": "a/1.jpg", "desc": "孤儿"}]      # 仍在清单里，只是没有号


def test_number_hit_images_leaves_upload_failure_unreferenced():
    """上传失败的图没有 key：ref 只发给真能取到的图，免得模型去引用一张渲染不出来的图。"""
    hits = [_hit(4, "【图片：传丢的图】", [{"key": None, "desc": "传丢的图"}])]
    images = _numbered(hits)
    assert images[0]["key"] is None
    assert "ref" not in images[0]


def test_number_hit_images_dedups_across_hits():
    hits = [_hit(13, "【图片：熊猫塔】", [{"key": "a/6.jpg", "desc": "熊猫塔"}]),
            _hit(18, "【图片：熊猫塔】", [{"key": "a/6.jpg", "desc": "熊猫塔"}])]
    assert len(_numbered(hits)) == 1


def test_number_hit_images_ref_is_stable_across_calls():
    """号只由 chunk id 与块内位置决定，不依赖调用次数或任何全局计数器。"""
    def build():
        return [_hit(13, "【图片：熊猫塔】", [{"key": "a/6.jpg", "desc": "熊猫塔"}])]
    assert _numbered(build())[0]["ref"] == _numbered(build())[0]["ref"] == "13-1"


def test_number_hit_images_scopes_refs_per_chunk():
    """不同 chunk 各自从 1 起，靠 chunk id 前缀区分，不会撞。"""
    hits = [_hit(13, "【图片：甲】", [{"key": "a/1.jpg", "desc": "甲"}]),
            _hit(17, "【图片：乙】", [{"key": "b/2.jpg", "desc": "乙"}])]
    assert [i["ref"] for i in _numbered(hits)] == ["13-1", "17-1"]
