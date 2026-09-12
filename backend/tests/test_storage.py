# Storage 客户端测试：key 编码、状态码分类、token 现签。
import httpx
import pytest

from app.core.storage import (BUCKET_FILES, StorageAuthError, StorageClient,
                              StorageRejected, encode_key, safe_ext)

from tests.fake_storage import TEST_SECRET, make_client


async def test_put_then_get_roundtrip():
    client, fake = make_client()
    await client.put("u1", BUCKET_FILES, "u1/c1/f1/original.xlsx", b"hello")
    assert fake.objects[(BUCKET_FILES, "u1/c1/f1/original.xlsx")] == b"hello"
    assert await client.get("u1", BUCKET_FILES, "u1/c1/f1/original.xlsx") == b"hello"


async def test_get_returns_none_only_on_404():
    client, _ = make_client()
    assert await client.get("u1", BUCKET_FILES, "nope") is None


async def test_put_rejects_mismatched_returned_key():
    """storage 回执里的 key 与请求不一致时必须报错：不校验等于把「悬空行」伪装成成功。"""
    client, fake = make_client()
    # 只在假后端上换掉写对象那一步，其余（鉴权、路径解析）仍走真实现
    fake._put = lambda bucket, key, request: httpx.Response(200, json={"Key": "别的桶/别的路径"})
    with pytest.raises(StorageRejected) as e:
        await client.put("u1", BUCKET_FILES, "a/b.txt", b"x")
    assert "别的路径" in str(e.value)


async def test_auth_error_on_bad_secret():
    """secret 不一致 → 401 → StorageAuthError（不是泛化异常）。"""
    client, _ = make_client(TEST_SECRET + "-right")
    client._secret = TEST_SECRET + "-wrong"
    try:
        await client.get("u1", BUCKET_FILES, "a/b.txt")
    except StorageAuthError:
        pass
    else:
        raise AssertionError("401 必须抛 StorageAuthError")


async def test_rls_denial_is_auth_error_not_rejected():
    """RLS 拒绝被 storage-api 包成 **HTTP 400**，真实状态码在 body 的 statusCode 里。

    只看 HTTP 状态码的话，策略拦下来的请求会落进 StorageRejected 的泛化分支，
    「有人动了 auth 密钥」那条 WARNING 永远不触发——而它正是这个告警存在的理由。
    """
    client, fake = make_client()
    fake._put = lambda bucket, key, request: httpx.Response(400, json={
        "statusCode": "403", "error": "Unauthorized", "code": "AccessDenied",
        "message": "new row violates row-level security policy for table \"objects\""})
    with pytest.raises(StorageAuthError):
        await client.put("u1", BUCKET_FILES, "u1/c1/a.txt", b"x")


async def test_encode_key_escapes_each_segment():
    """`?` 不编码就会变成 query string，对象落成截断名而表里记的是全长名 → 下载永远 404。"""
    assert encode_key("u1/c1/报表.x?x") == "u1/c1/%E6%8A%A5%E8%A1%A8.x%3Fx"
    client, fake = make_client()
    await client.put("u1", BUCKET_FILES, "u1/c1/报表.x?x", b"x")
    assert (BUCKET_FILES, "u1/c1/报表.x?x") in fake.objects


def test_safe_ext_drops_what_storage_api_rejects():
    """**对象 key 必须全 ASCII**：storage-api 对含非 ASCII 的 key 直接 400 InvalidKey。

    非 ASCII 文件名在这个项目里是常态（`测试数据.xlsx`），中文扩展名并非不可能。
    判不出来就丢——扩展名只给 key 一点可读性，真名在 `files.name`，丢它不影响下载。
    """
    assert safe_ext("报表.xlsx") == ".xlsx"
    assert safe_ext("a.XLSX") == ".xlsx"          # 统一降为小写
    assert safe_ext("报表.表格") == ""             # 非 ASCII 扩展名：真实 API 拒
    assert safe_ext("报表") == ""                  # 无扩展名
    assert safe_ext("a." + "x" * 20) == ""        # 超长，白名单封顶 8
    assert safe_ext("a.x#y") == ""                # `#` 真实 API 拒
    assert safe_ext("a.x?y") == ""                # `?` API 收，但白名单不收——够用即可


async def test_delete_prefix_removes_only_matching():
    client, fake = make_client()
    await client.put("u1", BUCKET_FILES, "u1/c1/a.txt", b"a")
    await client.put("u1", BUCKET_FILES, "u1/c1/b.txt", b"b")
    await client.put("u1", BUCKET_FILES, "u1/c2/c.txt", b"c")
    assert await client.delete_prefix("u1", BUCKET_FILES, "u1/c1/") == 2
    assert await client.get("u1", BUCKET_FILES, "u1/c2/c.txt") == b"c"


async def test_delete_prefix_walks_nested_dirs():
    """真实 key 是 `<user>/<conv>/<file_id>/original.<ext>`，比会话目录还深两层。

    list 是目录式列举（返回下一段路径名），只列一层会一个都删不到——
    而漏删不报错，表现是「会话删了，桶里留一堆孤儿」，要等到配额涨了才发现。
    """
    client, fake = make_client()
    await client.put("u1", BUCKET_FILES, "u1/c1/f1/original.xlsx", b"1")
    await client.put("u1", BUCKET_FILES, "u1/c1/f2/original.csv", b"2")
    await client.put("u1", BUCKET_FILES, "u1/c2/f3/original.txt", b"3")
    assert await client.delete_prefix("u1", BUCKET_FILES, "u1/c1/") == 2
    assert fake.objects == {(BUCKET_FILES, "u1/c2/f3/original.txt"): b"3"}


async def test_delete_prefix_pages_through_large_dirs(monkeypatch):
    """一个目录超过一页时不能只删第一页。

    分页靠 `offset`（实测可用）。它若不生效，同一个前缀会一直返回同一页 ——
    所以这个用例同时钉住「页数有限」，不会挂死。
    """
    from app.core import storage as storage_mod
    monkeypatch.setattr(storage_mod, "_PAGE_MAX", 2)
    client, fake = make_client()
    for n in range(5):
        await client.put("u1", BUCKET_FILES, f"u1/c1/{n}.txt", b"x")
    assert await client.delete_prefix("u1", BUCKET_FILES, "u1/c1/") == 5
    assert fake.objects == {}


async def test_token_is_minted_per_call():
    """**最容易踩的一条**：token 一旦缓存，后端启动 180 秒后全链路 401。

    断言方式是「实例上没有预先签好的 token 字段」，而不是比对两次调用的 iat——
    iat 是秒级精度，同一秒内两次调用本来就相同，断言它不复用会偶发失败。
    """
    import inspect
    client, _ = make_client()
    assert not hasattr(client, "token")
    assert "user_id" in inspect.signature(client._token).parameters
