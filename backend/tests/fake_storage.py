# tests/fake_storage.py
# 内存版 storage-api：给 StorageClient 接一个 httpx.MockTransport。
# 不手写 FakeStorage 对象：FileStore 的 key 拼接、百分号编码、状态码分支都要真实走一遍，
# 换个假对象就把这一层绕过去了——真出问题时的表现是「生产 404、测试全绿」。
import json
from urllib.parse import unquote

import httpx
import jwt

from app.core.storage import StorageClient


# 假签名密钥。刻意取够 32 字节：PyJWT 对更短的 HMAC 密钥每次签名/验签各发一条
# InsecureKeyLengthWarning，而它会在几十个用例里被触发，把真警告淹掉。
TEST_SECRET = "test-secret-that-is-long-enough-for-hs256"


class FakeStorage:
    """按 storage-api v1.74.0 的 REST 形状实现上传/下载/删除/批量删/列表。"""

    def __init__(self, secret: str = TEST_SECRET):
        self.secret = secret
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, str]] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # url.path 不含 query string：`?` 若没被 encode_key 编码，它后面的部分会跑进 query，
        # 这里的 key 就对不上——「编码契约」这条测试正是靠这一点立起来的
        path = unquote(request.url.path)
        self.calls.append((request.method, path))
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], audience="authenticated")
        except Exception:
            return httpx.Response(401, json={"message": "invalid token"})
        prefix = "/storage/v1/object/"
        if not path.startswith(prefix):
            return httpx.Response(404, json={"message": "not found"})
        rest = path[len(prefix):]
        if rest.startswith("authenticated/"):
            rest = rest[len("authenticated/"):]
        if rest.startswith("list/"):
            return self._list(rest[len("list/"):], json.loads(request.content or b"{}"))
        bucket, _, key = rest.partition("/")
        if key == "" and request.method == "DELETE":
            return self._delete_many(bucket, json.loads(request.content or b"{}"))
        if request.method == "POST":
            return self._put(bucket, key, request)
        if request.method == "GET":
            return self._get(bucket, key)
        if request.method == "DELETE":
            return self._delete(bucket, key)
        return httpx.Response(405)

    def _put(self, bucket, key, request) -> httpx.Response:
        self.objects[(bucket, key)] = request.content
        return httpx.Response(200, json={"Key": f"{bucket}/{key}"})

    def _get(self, bucket, key) -> httpx.Response:
        data = self.objects.get((bucket, key))
        if data is None:
            return httpx.Response(404, json={"message": "Object not found"})
        return httpx.Response(200, content=data)

    def _delete(self, bucket, key) -> httpx.Response:
        self.objects.pop((bucket, key), None)
        return httpx.Response(200, json={"message": "Successfully deleted"})

    def _delete_many(self, bucket, body) -> httpx.Response:
        """DELETE /object/{bucket}：body 的 `prefixes` 是**精确对象路径**列表，不是路径前缀。

        真 storage-api 传目录路径返回 `200 []` 且一个都不删。这里必须照抄这个行为——
        假后端「顺手」做了前缀匹配的话，测试全绿而线上一个对象都删不掉。
        """
        removed = []
        for p in body.get("prefixes", []):
            if self.objects.pop((bucket, p), None) is not None:
                removed.append({"name": p, "bucket_id": bucket})
        return httpx.Response(200, json=removed)

    def _list(self, bucket, body) -> httpx.Response:
        """POST /object/list/{bucket}：**目录式列举**，返回的是下一段路径名，不是完整 key。

        伪目录 `id` 为 null、真对象 `id` 是真 uuid —— 真 storage-api 靠这一条区分，
        `_collect` 的递归也依赖它。这里若返回完整 key，递归那层就永远走不到。
        """
        prefix = body.get("prefix", "")
        seen: dict[str, dict] = {}
        for (b, k) in sorted(self.objects):
            if b != bucket or not k.startswith(prefix):
                continue
            name, sep, _ = k[len(prefix):].partition("/")
            if name and name not in seen:
                seen[name] = {"name": name, "id": None if sep else f"id-{k}"}
        items = list(seen.values())
        offset, limit = body.get("offset", 0), body.get("limit", 100)
        return httpx.Response(200, json=items[offset:offset + limit])


def make_client(secret: str = TEST_SECRET):
    """造一个接在内存假 storage 上的客户端，返回 (client, fake)。"""
    fake = FakeStorage(secret)
    http = httpx.AsyncClient(transport=fake.transport)
    return StorageClient("http://gw:8000", secret, http=http), fake
