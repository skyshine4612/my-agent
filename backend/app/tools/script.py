# app/tools/script.py
# run_skill_script：在沙箱里执行一个仓库中已评审的数据处理脚本。
# 安全边界集中在三处：① (skill, script) 由白名单 + enum 钉死；② 脚本源码由后端读盘，不经模型的手；
# ③ params 里只有「等于已知 file_id」的字符串会被替换成沙箱内路径，其余原样传递、由脚本自己校验。
# 参数名不能叫 args：langchain 把同名字段当作可变参数的合成字段直接剔除，值永远送不进函数体。
import asyncio
import base64
import logging
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_conversation_id, current_user_id

logger = logging.getLogger(__name__)


def _substitute_file_ids(node, known, referenced):
    """递归替换 params 里的 file_id：命中已知值的换成沙箱内路径并记进 referenced，其余原样返回。

    「只替换等于已知 file_id 的字符串」本身就是参数注入的防线：模型填 `../../etc/passwd`
    只会原样传给脚本，脚本 open() 直接失败，不需要额外校验代码。
    """
    if isinstance(node, str):
        if node in known:
            referenced.add(node)
            return f"/tmp/work/{node}"
        return node
    if isinstance(node, list):
        return [_substitute_file_ids(v, known, referenced) for v in node]
    if isinstance(node, dict):
        return {k: _substitute_file_ids(v, known, referenced) for k, v in node.items()}
    return node


def make_run_skill_script(sandbox, file_store, scripts):
    """构造 run_skill_script 工具：闭包沙箱客户端、文件存储与脚本索引。

    依赖由 service 层闭包进来（照 app/tools/file.py 的 read_file/grep），工具本身不 import 全局单例。
    """
    skill_names = sorted({s for s, _ in scripts})
    script_names = sorted({n for _, n in scripts})
    # 空清单退化为 str：Literal[()] 会让 pydantic 建模抛异常，而 service 在 import 期构造，等于启动崩溃
    SkillName = Literal[tuple(skill_names)] if skill_names else str
    ScriptName = Literal[tuple(script_names)] if script_names else str

    @tool
    async def run_skill_script(
        skill: Annotated[SkillName, Field(description="业务名")],
        script: Annotated[ScriptName, Field(description="脚本名")],
        params: Annotated[dict, Field(description="脚本参数（字段含义见该业务的规则正文）")],
    ) -> dict:
        """执行一个已评审的数据处理脚本（如 Excel 字段脱敏）。可用的脚本与参数含义见对应业务的规则正文。"""
        path = scripts.get((skill, script))
        if path is None:
            return {"ok": False, "error": f"没有这个脚本：{skill}/{script}"}

        user_id = current_user_id.get()
        conversation_id = current_conversation_id.get()
        source = f"script:{skill}/{script}"

        # 解析 params 里的文件引用：换成沙箱内路径，同时把被引用的文件字节收齐
        known = {f["file_id"]: f for f in await file_store.list_files(user_id, conversation_id, limit=50)}
        referenced = set()
        resolved = _substitute_file_ids(params, known, referenced)
        files = []
        for fid in sorted(referenced):
            got = await file_store.read(user_id, conversation_id, fid)
            if got is None:
                return {"ok": False, "error": f"文件不存在或无权访问：{fid}"}
            files.append({"name": fid, "data": base64.b64encode(got[1]).decode()})

        # 脚本源码从白名单路径读盘：模型递进来的只有 skill/script 两个名字
        script_source = await asyncio.to_thread(path.read_text, encoding="utf-8")
        result = await sandbox.run(script_source, resolved, files)

        # 产物写回会话文件目录，生成新 file_id 供前端下载
        artifacts = [await file_store.save(user_id, conversation_id, a["name"],
                                           base64.b64decode(a["data"]), source=source)
                     for a in result.get("artifacts", [])]

        ok = result.get("exit_code") == 0
        out = {"ok": ok, "output": result.get("stdout", ""), "artifacts": artifacts}
        if not ok:
            out["error"] = result.get("stderr", "") or f"脚本退出码 {result.get('exit_code')}"
        return out

    return run_skill_script


def register_script_tools(registry, sandbox, file_store, scripts):
    """把 run_skill_script 注入 ToolRegistry（需要沙箱客户端与文件存储，故在 service 层装配）。"""
    registry.register_tool(make_run_skill_script(sandbox, file_store, scripts), "执行数据处理脚本")
