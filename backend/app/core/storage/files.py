# app/core/storage/files.py
# 会话文件存储：字节在 Supabase Storage 的 files 桶，元数据在 files 表。
# key 约定 <user_id>/<conversation_id>/<file_id>/original<后缀>：前两段让「按会话前缀删」
# 一次到位（不读表），第三段是防穿越校验过的 uuid。
# 与 result_dir（spill，本轮结束即清）是两棵树：这里的东西活到会话结束。
import logging
import re
import uuid
from pathlib import Path

from app.core.db import rls_conn
from app.core.storage.client import BUCKET_FILES, safe_ext

logger = logging.getLogger(__name__)

# 净化后的文件名上限：清单渲染与沙箱 work_dir 都不需要更长的名字，
# 而 UTF-8 中文一个字 3 字节，80 字已接近常见文件系统 255 字节的边。
MAX_NAME_CHARS = 80
DEFAULT_NAME = "未命名文件"
# 按文件名反查时的扫描上限。必须 ≥ 清单工具（list_uploaded_files）的 limit，
# 否则会出现「模型在清单里看得见这个名字、却怎么也解析不到」的死角。
FILE_REF_LIMIT = 50


def safe_filename(name: str) -> str:
    """把任意来源的名字净化成裸文件名：只留末段、只留可打印字符、限长保后缀。

    末段是因为 posix 与 windows 路径都可能混进来；剔控制字符是因为这个名字要进清单给模型看，
    换行会把清单的一行拆成两行。**只在 save 时净化一次**，之后全链路（清单渲染、替换匹配、
    Storage key、脚本定位）用的都是这一个名字 —— 净化点有两处就会有两个不同的名字。
    """
    if not name:
        return DEFAULT_NAME
    tail = re.split(r"[/\\]", name)[-1]
    tail = "".join(ch for ch in tail if ch.isprintable()).strip()
    if not tail:
        return DEFAULT_NAME
    if len(tail) > MAX_NAME_CHARS:
        suffix = Path(tail).suffix
        if len(suffix) >= MAX_NAME_CHARS:
            suffix = ""
        tail = tail[:MAX_NAME_CHARS - len(suffix)] + suffix
    return tail


class FileStore:
    """会话文件存储：用户上传件与沙箱产物共用一套语义。"""

    def __init__(self, storage, bucket=BUCKET_FILES):
        self.storage = storage
        self.bucket = bucket

    @staticmethod
    def _check_file_id(file_id):
        """确认 file_id 是合法 uuid。

        它来自请求（模型填的 params 或前端 URL），**这是防穿越的唯一一道校验**：
        以前它拼的是磁盘路径，现在拼的是 Storage key，但「请求里的字符串直接进 key」没变。
        """
        try:
            uuid.UUID(file_id)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("非法 file_id")
        return file_id

    def _key(self, user_id, conversation_id, file_id, name) -> str:
        """`safe_ext` 而不是 `Path(name).suffix`：key 必须全 ASCII（见 `safe_ext` 注释）。"""
        return f"{user_id}/{conversation_id}/{file_id}/original{safe_ext(name)}"

    @staticmethod
    def _meta(row) -> dict:
        """行 → 旧 meta.json 同形的 dict。

        必须给全 name/size/source/created_at：下载路由要用 meta['name'] 拼
        Content-Disposition，只给 storage_path 会 KeyError。
        created_at 显式 isoformat()，与旧 meta.json 同形。
        """
        return {"name": row["name"], "size": row["size"], "source": row["source"],
                "created_at": row["created_at"].isoformat()}

    async def save(self, user_id, conversation_id, name, data, source="upload"):
        """存一个文件，返回 {file_id, name, size}。source 取 "upload" 或 "script:<skill>/<script>"。

        先 put 后 insert，**行是提交点**：insert 失败就补偿删掉刚写的对象。
        反过来（先 insert）留下的失败态是「无人引用的字节」的孪生兄弟——「清单里看得见、
        read 却返回 None」的悬空行，模型会拿到自相矛盾的事实。
        """
        file_id = uuid.uuid4().hex
        name = safe_filename(name)
        key = self._key(user_id, conversation_id, file_id, name)
        await self.storage.put(user_id, self.bucket, key, data)
        try:
            async with rls_conn(user_id) as conn:
                await conn.execute(
                    "insert into files(id, user_id, conversation_id, name, size, source, storage_path) "
                    "values($1, $2, $3, $4, $5, $6, $7)",
                    file_id, user_id, conversation_id, name, len(data), source, key)
        except Exception:
            logger.error("[文件] 写入 files 行失败，回滚对象 key=%s", key, exc_info=True)
            try:
                await self.storage.delete(user_id, self.bucket, key)
            except Exception:
                # 补偿失败只记日志：不能让它盖掉下面重新抛出的原始异常
                logger.warning("[文件] 补偿删除对象失败，留作孤儿 key=%s", key, exc_info=True)
            raise
        logger.info("[文件] 落盘：%s（%s，%d 字节）", name, source, len(data))
        return {"file_id": file_id, "name": name, "size": len(data)}

    async def read(self, user_id, conversation_id, file_id):
        """读回 (meta, 字节)；file_id 非法、行不存在或对象不存在都返回 None。"""
        try:
            self._check_file_id(file_id)
        except ValueError:
            return None
        async with rls_conn(user_id) as conn:
            row = await conn.fetchrow(
                "select name, size, source, storage_path, created_at from files "
                "where id = $1 and user_id = $2 and conversation_id = $3",
                file_id, user_id, conversation_id)
        if row is None:
            return None
        data = await self.storage.get(user_id, self.bucket, row["storage_path"])
        if data is None:
            return None
        return self._meta(row), data

    async def list_files(self, user_id, conversation_id, limit=10):
        """列出会话内最近 limit 个文件（新→旧），供 list_uploaded_files 清单与 list_by_name 反查。"""
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select id, name, size, source, storage_path, created_at from files "
                "where user_id = $1 and conversation_id = $2 "
                # created_at 是 now()（事务开始时间），同一事务内多行同值；
                # id desc 把 tie-break 定死，否则同毫秒写入的先后不定序
                "order by created_at desc, id desc limit $3",
                user_id, conversation_id, limit)
        return [{"file_id": r["id"], **self._meta(r)} for r in rows]

    async def list_by_name(self, user_id, conversation_id, limit=FILE_REF_LIMIT):
        """按文件名反查，返回 {净化名: 条目}，插入顺序＝新→旧。

        「同名取最新」在这一个地方收口：清单渲染与沙箱参数替换都从这里取。
        各写一遍，就会出现「模型看到的名字」与「实际解析到的文件」不是同一个。

        limit 必须施加在**子查询**里：挪到外层，「参与去重的集合」就从「最近 50 个文件」
        变成「字母序前 50 个名字」。
        """
        async with rls_conn(user_id) as conn:
            rows = await conn.fetch(
                "select distinct on (name) id, name, size, source, storage_path, created_at "
                "from (select * from files where user_id = $1 and conversation_id = $2 "
                "      order by created_at desc, id desc limit $3) t "
                "order by name, created_at desc, id desc",
                user_id, conversation_id, limit)
        out = {r["name"]: {"file_id": r["id"], **self._meta(r)} for r in rows}
        # distinct on 要求外层 order by 以 name 打头，而 render_file_directory 按
        # entries.items() 的顺序出清单，所以这里按 created_at 再倒序排一次
        return dict(sorted(out.items(), key=lambda kv: kv[1]["created_at"], reverse=True))

    async def delete_conversation(self, user_id, conversation_id):
        """删本会话在 Storage 里的全部对象；行交给 conversations 级联删除。

        **不读表**：读表的方案要求行还在，而调用方（AgentService.delete_conversation）
        必须先把这一步跑完再删会话——任何一次顺序松动都会让对象永不回收。
        删失败只记日志、照常返回：会话已经删了，这里抛异常会让 DELETE 路由 500，
        客户端重试时 belongs_to 已为假 → 前端永远显示「删除失败」。
        """
        prefix = f"{user_id}/{conversation_id}/"
        try:
            n = await self.storage.delete_prefix(user_id, self.bucket, prefix)
            logger.info("[文件] 清理会话对象：%s（%d 个）", conversation_id, n)
        except Exception:
            logger.warning("[文件] 清理会话对象失败，留作孤儿 prefix=%s", prefix, exc_info=True)
