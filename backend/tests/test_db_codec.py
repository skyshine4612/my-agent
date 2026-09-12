# jsonb codec：asyncpg 默认把 json/jsonb 按**文本**编解码，不注册 codec 的话
# hits[i]["images"] 会是字符串，下游（图片归属、前端下发）全部落空 ——
# 而 FakeStorage 单测照不到这一层。
from app.core.db import admin_conn, rls_conn


async def test_jsonb_comes_back_as_list(db, user_id):
    """传 Python 对象、不传 JSON 字符串：注册了 encoder 之后再手写 JSON 会被双重编码，
    读回来是个「长得像 JSON 的 str」，看着像没注册 codec。"""
    async with admin_conn() as conn:
        await conn.execute("insert into documents(title, content, embedding, images) "
                           "values('t', 'c', array_fill(0, array[1024])::vector, $1)",
                           [{"key": "a/b/0.jpg", "desc": "西湖"}])
    async with rls_conn(user_id) as conn:
        row = await conn.fetchrow("select images from documents limit 1")
    assert isinstance(row["images"], list)
    assert row["images"][0]["desc"] == "西湖"


async def test_old_rows_keep_null_images(db, user_id):
    """NULL 专属旧行：区分「历史上没这列」与「这篇没图」。"""
    async with admin_conn() as conn:
        await conn.execute("insert into documents(title, content, embedding) "
                           "values('t', 'c', array_fill(0, array[1024])::vector)")
    async with rls_conn(user_id) as conn:
        row = await conn.fetchrow("select images from documents limit 1")
    assert row["images"] is None
