# app/core/files.py
# 会话文件存储：file_dir/<user_id>/<conversation_id>/<file_id>/ 一个原件 + 一份 meta.json。
# 一文件一目录而非 <file_id>.xlsx 平铺，是为了避开「原始文件名存哪」：存进文件名要处理特殊字符与长度，
# 存进共享索引要处理并发写；meta.json 各写各的，没有竞争。
# 与 result_dir（spill，本轮结束即清）是两棵树：这里的东西活到会话结束。
import asyncio
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

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
    脚本定位）用的都是这一个名字 —— 净化点有两处就会有两个不同的名字。
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
    """会话文件存储：用户上传件与沙箱产物共用一套目录与语义。"""

    def __init__(self, file_dir):
        self.file_dir = Path(file_dir)

    def _conversation_dir(self, user_id, conversation_id) -> Path:
        return self.file_dir / user_id / conversation_id

    def _file_dir(self, user_id, conversation_id, file_id) -> Path:
        """把 file_id 解析成会话目录下的子目录；非法 uuid 一律拒绝。

        file_id 来自请求（模型填的 params 或前端 URL），拼路径前必须先确认它是合法 uuid——
        这是防路径穿越的唯一一道校验，缺了就等于开放 file_dir 之外的任意路径读写。
        """
        try:
            uuid.UUID(file_id)
        except (ValueError, AttributeError, TypeError):
            raise ValueError("非法 file_id")
        return self._conversation_dir(user_id, conversation_id) / file_id

    async def save(self, user_id, conversation_id, name, data, source="upload"):
        """存一个文件，返回 {file_id, name, size}。source 取 "upload" 或 "script:<skill>/<script>"。"""
        file_id = uuid.uuid4().hex
        name = safe_filename(name)
        target = self._conversation_dir(user_id, conversation_id) / file_id
        meta = {"name": name, "size": len(data), "source": source,
                "created_at": datetime.now(timezone.utc).isoformat()}
        suffix = Path(name).suffix.lower()

        def w():
            target.mkdir(parents=True, exist_ok=True)
            (target / f"original{suffix}").write_bytes(data)
            (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

        await asyncio.to_thread(w)
        logger.info("[文件] 落盘：%s（%s，%d 字节）", name, source, len(data))
        return {"file_id": file_id, "name": name, "size": len(data)}

    async def read(self, user_id, conversation_id, file_id):
        """读回 (meta, 字节)；file_id 非法、文件不存在或不属于该会话都返回 None。"""
        try:
            target = self._file_dir(user_id, conversation_id, file_id)
        except ValueError:
            return None

        def r():
            meta_path = target / "meta.json"
            if not meta_path.exists():
                return None
            originals = sorted(target.glob("original*"))
            if not originals:
                return None
            return json.loads(meta_path.read_text(encoding="utf-8")), originals[0].read_bytes()

        return await asyncio.to_thread(r)

    async def list_files(self, user_id, conversation_id, limit=10):
        """列出会话内最近 limit 个文件（新→旧），供 list_uploaded_files 清单与 list_by_name 反查。"""
        base = self._conversation_dir(user_id, conversation_id)

        def r():
            if not base.exists():
                return []
            out = []
            for d in base.iterdir():
                meta_path = d / "meta.json"
                if not d.is_dir() or not meta_path.exists():
                    continue
                out.append({"file_id": d.name, **json.loads(meta_path.read_text(encoding="utf-8"))})
            out.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return out[:limit]

        return await asyncio.to_thread(r)

    async def list_by_name(self, user_id, conversation_id, limit=FILE_REF_LIMIT):
        """按文件名反查，返回 {净化名: 条目}，插入顺序＝新→旧。

        `list_files` 已是这个手法（扫目录 + 读 meta.json），这里只是换个索引维度。
        没有共享索引可写，所以纯读、无并发问题 —— 见本文件头部的选型说明。

        「同名取最新」在这一个地方收口：清单渲染与沙箱参数替换都从这里取。
        各写一遍，就会出现「模型看到的名字」与「实际解析到的文件」不是同一个。
        """
        out = {}
        # list_files 已按 created_at 倒序（新→旧），setdefault 保住首次写入；裸推导式是后写覆盖，旧件会赢
        for f in await self.list_files(user_id, conversation_id, limit=limit):
            out.setdefault(f["name"], f)
        return out

    async def delete_conversation(self, user_id, conversation_id):
        """删 file_dir/<user_id>/<conversation_id>/ 整棵。"""
        target = self._conversation_dir(user_id, conversation_id)
        user_dir = self.file_dir / user_id

        def r():
            if target.exists():
                shutil.rmtree(target)
            # 会话目录删完后，user_id 目录空了才删；非空（还有其他会话）时 rmdir 抛 OSError 跳过
            try:
                user_dir.rmdir()
            except OSError:
                pass

        await asyncio.to_thread(r)
        logger.info("[文件] 清理会话文件目录：%s", conversation_id)
