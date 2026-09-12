# tests/test_knowledge.py
# 知识库集成测试：切块入库 + 语义检索 + 跨用户共享，走真实 Postgres 的 documents 表。
# 向量由 FakeEmbedder 给（见 conftest），绕开真实 embedding API；排序可预期，真实语义质量由端到端验证。
from app.core.knowledge import KnowledgeStore

# 段落各自成块的配置：短段落会被合并，想验「一块一段」的用例就把 chunk_size 压到段落长度量级
_PER_PARA = {"chunk_size": 12, "overlap": 0}


async def test_ingest_chunks_and_lists(db, user_id, fake_embedder):
    """多段文本按段落切块入库，list_documents 能按来源聚合出该文档。"""
    store = KnowledgeStore(embedder=fake_embedder, **_PER_PARA)
    n = await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。\n\n长城很长。",
                           title="杭州攻略", source_url="http://example.com/hz")
    assert n == 3

    docs = await store.list_documents(user_id)
    assert len(docs) == 1
    assert docs[0]["title"] == "杭州攻略"
    assert docs[0]["chunks"] == 3


async def test_short_paragraphs_land_in_one_chunk(db, user_id, fake_embedder):
    """默认配置下短段落合并成一个片段：块数不随段落数线性上涨。"""
    store = KnowledgeStore(embedder=fake_embedder)
    n = await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。\n\n长城很长。", title="攻略")
    assert n == 1


async def test_ingest_empty_text_writes_nothing(db, user_id, fake_embedder):
    """纯空白文本切不出块，不写库也不报错。"""
    store = KnowledgeStore(embedder=fake_embedder)
    assert await store.ingest(user_id, "   \n\n  ", title="空") == 0
    assert await store.list_documents(user_id) == []


async def test_search_ranks_by_similarity(db, user_id, fake_embedder):
    """检索按余弦相似度降序（问西湖，西湖那块排第一），且 top_k 限制返回条数。"""
    store = KnowledgeStore(embedder=fake_embedder, **_PER_PARA)
    await store.ingest(user_id, "西湖值得去。\n\n火锅值得吃。\n\n长城值得爬。", title="攻略")

    hits = await store.search(user_id, "西湖怎么玩", top_k=3)
    assert len(hits) == 3
    assert "西湖" in hits[0]["content"]
    assert hits[0]["similarity"] > hits[1]["similarity"]
    # top_k 生效：库里有 3 条，要 1 条就只给 1 条
    assert len(await store.search(user_id, "西湖", top_k=1)) == 1


async def test_search_empty_library(db, user_id, fake_embedder):
    """空库检索返回空列表，不报错。"""
    assert await KnowledgeStore(embedder=fake_embedder).search(user_id, "西湖", top_k=3) == []


async def test_knowledge_is_shared_across_users(db, user_id, make_user, fake_embedder):
    """知识库是全局共享的：A 入库的内容，B 也能检索到（策略是 using(true)，不按 user 隔离）。"""
    store = KnowledgeStore(embedder=fake_embedder)
    other = await make_user()
    await store.ingest(user_id, "西湖的断桥在冬天最美。", title="杭州攻略")

    hits = await store.search(other, "西湖", top_k=3)
    assert len(hits) == 1
    assert hits[0]["title"] == "杭州攻略"


async def test_delete_by_source_removes_all_chunks(db, user_id, fake_embedder):
    """按来源删除会清掉该文档的所有块，不留残片。"""
    store = KnowledgeStore(embedder=fake_embedder, **_PER_PARA)
    await store.ingest(user_id, "西湖很好玩。\n\n火锅也很好吃。", source_url="http://example.com/a")
    await store.ingest(user_id, "长城很长。", source_url="http://example.com/b")

    assert await store.delete_by_source(user_id, "http://example.com/a") == 2
    docs = await store.list_documents(user_id)
    assert len(docs) == 1
    assert docs[0]["source_url"] == "http://example.com/b"
    assert docs[0]["chunks"] == 1
