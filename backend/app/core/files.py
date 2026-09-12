# app/core/files.py
# 会话文件存储：file_dir/<user_id>/<conversation_id>/<file_id>/ 一个原件 + 一份 meta.json。
# 一文件一目录而非 <file_id>.xlsx 平铺，是为了避开「原始文件名存哪」：存进文件名要处理特殊字符与长度，
# 存进共享索引要处理并发写；meta.json 各写各的，没有竞争。
# 与 result_dir（spill，本轮结束即清）是两棵树：这里的东西活到会话结束。
import asyncio
import json
import logging
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


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
        """列出会话内最近 limit 个文件（新→旧），供 system prompt 的「本会话可用文件」清单。"""
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
