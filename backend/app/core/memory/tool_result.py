# app/core/memory/tool_result.py
# 工具结果「地址索引」存储：完整工具结果写临时文件 result_dir/<user_id>/<conversation_id>/<uuid>.txt，
# read_file / grep 工具按路径读片段/搜索（对齐 Claude Code 的读文件形式，模型熟悉）。
# 生命周期：spill 文件本轮结束即清（保留 _history.txt）；会话删除时删整个目录（含 _history.txt）。
import asyncio
import re
import shutil
import uuid
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ToolResultStore:
    """工具结果地址索引：超长结果写临时文件，read_file/grep 按相对路径读，按 user_id 隔离目录。"""

    def __init__(self, result_dir):
        self.result_dir = Path(result_dir)

    def _safe_path(self, user_id, path) -> Path:
        """把 write 返回的相对路径解析到 result_dir/<user_id>/ 下的文件，拒绝路径穿越。

        path 形如 "<conversation_id>/<rid>.txt"；绝对路径、含 ".." 或解析后越出
        result_dir/<user_id>/ 的，一律抛 ValueError（read_file/grep 工具据此拒绝访问）。
        """
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("非法路径")
        base = (self.result_dir / user_id).resolve()
        full = (base / p).resolve()
        if not str(full).startswith(str(base)):
            raise ValueError("路径越界")
        return full

    async def write(self, conversation_id, user_id, content):
        """写入完整结果，返回相对路径 "<conversation_id>/<rid>.txt"。"""
        rid = uuid.uuid4().hex
        rel_path = f"{conversation_id}/{rid}.txt"
        full = self.result_dir / user_id / rel_path

        def r():
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content, encoding="utf-8")

        await asyncio.to_thread(r)
        logger.info("[记忆:tool_result] 完整结果 spill 写文件：%s（%d 字符）", rel_path, len(content))
        return rel_path

    async def read(self, path, offset, limit, user_id=None):
        """读文件 [offset:offset+limit]（字符），返回 {content, next_offset, total, eof} 或 None。"""
        try:
            full = self._safe_path(user_id, path)
        except ValueError:
            return None

        def r():
            if not full.exists():
                return None
            content = full.read_text(encoding="utf-8")
            total = len(content)
            seg = content[offset:offset + limit]
            next_offset = offset + len(seg)
            return {"content": seg, "next_offset": next_offset, "total": total, "eof": next_offset >= total}

        return await asyncio.to_thread(r)

    async def search(self, path, pattern, user_id=None, context=100):
        """在文件里正则搜 pattern，返回 {matches: [{match, offset, context}], count}。

        每个命中给匹配内容 + 字符位置 + 前后各 context 字符的上下文片段，
        模型/critic 可「grep 定位 offset → read_file(offset) 读上下文」组合使用。
        """
        try:
            full = self._safe_path(user_id, path)
        except ValueError:
            return {"matches": [], "count": 0}

        def r():
            if not full.exists():
                return {"matches": [], "count": 0}
            content = full.read_text(encoding="utf-8")
            matches = []
            try:
                for m in re.finditer(pattern, content):
                    start = max(0, m.start() - context)
                    end = min(len(content), m.end() + context)
                    matches.append({"match": m.group(0), "offset": m.start(),
                                    "context": content[start:end]})
            except re.error:
                return {"matches": [], "count": 0, "error": f"非法正则: {pattern}"}
            return {"matches": matches, "count": len(matches)}

        return await asyncio.to_thread(r)

    async def append_history(self, conversation_id, user_id, content):
        """把被压缩的答案追加到会话的完整历史文件，返回相对路径 <conversation_id>/_history.txt。"""
        rel_path = f"{conversation_id}/_history.txt"
        full = self.result_dir / user_id / rel_path

        def r():
            full.parent.mkdir(parents=True, exist_ok=True)
            with open(full, "a", encoding="utf-8") as f:
                f.write(content + "\n\n")

        await asyncio.to_thread(r)
        logger.info("[记忆:tool_result] 被压缩答案追加到历史文件：%s（+%d 字符）", rel_path, len(content))
        return rel_path

    async def clear_spill(self, conversation_id, user_id):
        """删除该会话目录下的 spill 文件（<uuid>.txt），保留 _history.txt（短期记忆完整历史）。

        用于每轮结束清理：spill 文件 read 完即弃，但 _history.txt 是短期记忆的「存文件」部分，需跨轮保留。
        """
        dir_path = self.result_dir / user_id / conversation_id

        def r():
            if not dir_path.exists():
                return
            for f in dir_path.iterdir():
                if f.is_file() and f.name != "_history.txt":
                    f.unlink()

        await asyncio.to_thread(r)
        logger.info("[记忆:tool_result] 清理会话 spill 文件（保留 _history.txt）：%s", conversation_id)

    async def delete_conversation(self, conversation_id, user_id):
        """删 result_dir/<user_id>/<conversation_id>/ 目录（删除会话时调用，含 _history.txt）。

        顺带清理空的 user_id 目录，避免残留空目录。
        """
        dir_path = self.result_dir / user_id / conversation_id
        user_dir = self.result_dir / user_id

        def r():
            if dir_path.exists():
                shutil.rmtree(dir_path)
            # 会话目录删完后，若 user_id 目录已空则一并删掉；非空（还有其他会话）则 rmdir 抛 OSError 跳过
            try:
                user_dir.rmdir()
            except OSError:
                pass

        await asyncio.to_thread(r)
        logger.info("[记忆:tool_result] 清理会话地址索引目录：%s", conversation_id)
