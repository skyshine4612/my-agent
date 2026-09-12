# app/core/storage.py
# Supabase Storage 客户端：上传/下载/删除对象。
# 身份用「每次调用内现签的短时 JWT」——RLS 判定发生在 storage-api 进程里，不是后端进程里。
# 后端没法「设置」它的会话状态（rls_conn 能直说是因为后端自己就是那个 PG 连接的主人），
# 对 storage-api 说不了，它只认签名。它能取的 auth.uid() 唯一来源就是请求头里的 JWT。
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

logger = logging.getLogger(__name__)

# 桶名写死常量：只有两个，不做成配置项
BUCKET_FILES = "files"
BUCKET_KNOWLEDGE = "knowledge"

# 单次调用用的 token 寿命：够一次上传/下载，又短到泄漏了几乎没用。
# 按**单次调用**而非整个任务计算。
TOKEN_TTL_SECONDS = 180

# 列举与批量删的单页条数：批量删单请求上限就是 1000，取满则一页一次请求删完。
# 实测 `list` 接受 limit=1000 且 `offset` 生效（分页靠它，不生效会死循环）。
_PAGE_MAX = 1000


class StorageError(Exception):
    """Storage 链路异常基类。"""


class StorageAuthError(StorageError):
    """401/403：凭据被拒。"""


class StorageNotFound(StorageError):
    """404：对象不存在。"""


class StorageRejected(StorageError):
    """其余非 2xx 状态码。"""


def encode_key(key: str) -> str:
    """把对象 key 的每个 path 段独立百分号编码，段内一律 safe=""。

    `safe_filename` 只剔控制字符，`?`、`#`、`%`、中文都保留，而脚本产物名不受 `.xlsx` 限制——
    `报表.x?x` 裸拼进 URL 会被当成 query string，对象落成截断名而表里记的是全长名，
    下载永远 404，截断名对象成孤儿。段分隔符 `/` 本身不进编码（靠 split/join 保住）。
    """
    return "/".join(quote(seg, safe="") for seg in key.split("/"))


# 扩展名白名单：小写字母数字，长度封顶。够 cover xlsx / csv / png / jpeg 这些。
_EXT_OK = re.compile(r"\A[a-z0-9]{1,8}\Z")


def safe_ext(name: str) -> str:
    """取 `name` 的扩展名（含点）；判不出安全扩展名时返回空串。

    **对象 key 必须全 ASCII，且不能含 `#` / `%`**：storage-api v1.74.0 对这些直接
    `400 InvalidKey`，百分号编码救不了（实测中文扩展名、中文目录段、`#`、`%` 一律被拒；
    `?`、空格、大写、超长扩展名反而收）。

    扩展名只给 key 一点可读性，不是下载依据——真名在 `files.name`，`Content-Disposition` 用它。
    所以判不出来时丢掉即可，**不要退回原串**：退回就是把 400 又引回来。
    """
    ext = Path(name).suffix.lower().lstrip(".")
    return f".{ext}" if _EXT_OK.match(ext) else ""


def _json_or_empty(resp: httpx.Response):
    """尽力解析 JSON 响应体；解析不出返回 None。"""
    try:
        return resp.json()
    except Exception:
        return None


def _is_denied(resp: httpx.Response) -> bool:
    """判断这次失败是不是凭据/策略被拒。

    storage-api 把 RLS 拒绝包成 **HTTP 400**，真实状态码在 body 的 `statusCode` 里
    （`{"statusCode":"403","code":"AccessDenied"}`）。只看 HTTP 状态码的话，
    策略拦下来的请求会落进 StorageRejected 的泛化分支，
    「有人动了 auth 密钥」那条告警永远不触发。

    body 判定是**追加**的，不是替代：401/403 本身就算被拒，它们不保证带 body。
    """
    if resp.status_code in (401, 403):
        return True
    if resp.status_code != 400:
        return False
    body = _json_or_empty(resp) or {}
    return str(body.get("statusCode", "")) in ("401", "403") \
        or body.get("code") in ("AccessDenied", "InvalidJWT", "Unauthorized")


class StorageClient:
    """Supabase Storage 的 REST 客户端，只做本项目用到的五个动作。"""

    def __init__(self, base_url: str, jwt_secret: str, http: httpx.AsyncClient | None = None):
        # base_url 传**网关地址**（不是 Postgres 连接串），Storage REST 挂在 /storage/v1 下
        self.base = base_url.rstrip("/") + "/storage/v1"
        self._secret = jwt_secret
        # http 可注入（测试接 MockTransport）；httpx 构造不开连接，仍是零 IO
        self._http = http or httpx.AsyncClient(timeout=30.0)
        self._own_http = http is None

    def _token(self, user_id: str) -> str:
        """现签一个短时 JWT。

        **只在方法内现签、只在局部变量里活**——绝不挂到 self、绝不缓存。
        本对象是启动时 new 一次的常驻对象，签名一旦写进 __init__，
        后端启动 180 秒后全链路 401：现象是「文件功能挂了」，根因是 token 过期。
        payload 与 rls_conn 注入的 claims 同一份（sub + role）。
        """
        now = int(time.time())
        return jwt.encode(
            {"sub": user_id, "role": "authenticated", "aud": "authenticated",
             "iat": now, "exp": now + TOKEN_TTL_SECONDS},
            self._secret, algorithm="HS256")

    async def _request(self, method: str, user_id: str, path: str, **kw) -> httpx.Response:
        """发一次请求并把「被拒」统一转成 StorageAuthError；其余状态码交给调用方判断。

        判据是 `_is_denied` 而不是 HTTP 状态码：storage-api 把 RLS 拒绝包成 400。

        follow_redirects=False：网关不做重定向，跟着跳只会把 Authorization 带到别处。
        """
        headers = {"Authorization": f"Bearer {self._token(user_id)}"}
        headers.update(kw.pop("headers", {}))
        resp = await self._http.request(method, self.base + path, headers=headers,
                                        follow_redirects=False, **kw)
        if _is_denied(resp):
            # storage 链路被拒就是「有人动了 auth 密钥」的第一信号：记进 CLAUDE.md 不变量 3
            logger.warning("[存储] 凭据被拒（HTTP %d）%s %s：若刚改过 auth 密钥配置，"
                           "见 SUPABASE_JWT_SECRET 与「必须保持 HS256 对称密钥」的前提",
                           resp.status_code, method, path)
            raise StorageAuthError(
                f"Storage 拒绝访问（{resp.status_code} {resp.text[:200]}）：{method} {path}")
        return resp

    @staticmethod
    def _check(resp: httpx.Response, path: str) -> None:
        """把剩余的非 2xx 状态码转成异常（401/403 已在 _request 里拦掉）。"""
        if resp.status_code == 404:
            raise StorageNotFound(path)
        if not resp.is_success:
            raise StorageRejected(f"{resp.status_code} {resp.text[:200]}（{path}）")

    async def put(self, user_id: str, bucket: str, key: str, data: bytes,
                  content_type: str = "application/octet-stream") -> None:
        """上传一个对象；**校验响应状态与回执里的 key**——不检查等于把「悬空行」伪装成成功。"""
        resp = await self._request("POST", user_id, f"/object/{bucket}/{encode_key(key)}",
                                   content=data, headers={"Content-Type": content_type})
        self._check(resp, key)
        # storage-api 回的是 {"Key": "<bucket>/<path>"}
        returned = (_json_or_empty(resp) or {}).get("Key", "")
        if returned and returned != f"{bucket}/{key}":
            raise StorageRejected(f"Storage 回执的 key 与请求不一致：{returned} != {bucket}/{key}")

    async def get(self, user_id: str, bucket: str, key: str) -> bytes | None:
        """取对象；**仅 404 返回 None**，其余状态码抛异常。

        私有桶的下载路径是 /object/authenticated/，不带 authenticated 会被当成匿名读。
        """
        resp = await self._request(
            "GET", user_id, f"/object/authenticated/{bucket}/{encode_key(key)}")
        if resp.status_code == 404:
            return None
        self._check(resp, key)
        return resp.content

    async def delete(self, user_id: str, bucket: str, key: str) -> None:
        """删单个对象（save 失败时的补偿）。对象本来就不在时视为成功。"""
        resp = await self._request("DELETE", user_id, f"/object/{bucket}/{encode_key(key)}")
        if resp.status_code == 404:
            return
        self._check(resp, key)

    async def delete_prefix(self, user_id: str, bucket: str, prefix: str) -> int:
        """删掉 prefix 下的全部对象，返回删掉的对象数。

        **storage-api 没有「按路径前缀删」的端点。** `DELETE /object/{bucket}` 的 body 字段
        虽然叫 `prefixes`，但要的是一串**精确对象路径**（S3 DeleteObjects 的 key 列表语义），
        传目录路径返回 `200 []` 且一个都不删 —— 不报错的静默空转。所以只能自己列。
        """
        keys = await self._collect(user_id, bucket, prefix)
        for i in range(0, len(keys), _PAGE_MAX):      # 单请求上限 1000
            resp = await self._request("DELETE", user_id, f"/object/{bucket}",
                                       json={"prefixes": keys[i:i + _PAGE_MAX]})
            self._check(resp, prefix)
        return len(keys)

    async def _collect(self, user_id: str, bucket: str, prefix: str) -> list[str]:
        """递归列出 prefix 下的全部对象 key。

        `list` 是目录式列举：返回下一段路径名、不是完整 key，靠 `id` 是否为空区分
        伪目录与真对象，所以完整 key 要自己拼、目录要自己下钻。漏一层就整批漏删，
        而漏删不报错（见 `test_delete_prefix_walks_nested_dirs`）。
        """
        out, offset = [], 0
        while True:
            page = await self.list(user_id, bucket, prefix, limit=_PAGE_MAX, offset=offset)
            for it in page:
                key = prefix + it["name"]
                if it.get("id") is None:
                    out.extend(await self._collect(user_id, bucket, key + "/"))
                else:
                    out.append(key)
            if len(page) < _PAGE_MAX:
                return out
            offset += len(page)

    async def list(self, user_id: str, bucket: str, prefix: str,
                   limit: int = 100, offset: int = 0) -> list[dict]:
        """列对象。**目录式列举**：返回下一段路径名，不是完整 key。

        伪目录 `id` 为 `null`、真对象 `id` 是真 uuid。`offset` 已实测可用（分页靠它）。
        """
        resp = await self._request("POST", user_id, f"/object/list/{bucket}",
                                   json={"prefix": prefix, "limit": limit, "offset": offset})
        self._check(resp, prefix)
        return _json_or_empty(resp) or []

    async def close(self) -> None:
        """关掉自有的 httpx 客户端；测试注入的那个由测试自己关。"""
        if self._own_http:
            await self._http.aclose()
