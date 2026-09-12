# app/tools/script.py
# run_skill_script：在沙箱里执行一个仓库中已评审的数据处理脚本。
# 安全边界集中在三处：① (skill, script) 由白名单 + enum 钉死；② 脚本源码由后端读盘，不经模型的手；
# ③ params 里只有「等于本会话某个文件名」的字符串会被认出来，其余原样传递、由脚本自己校验。
# 参数名不能叫 args：langchain 把同名字段当作可变参数的合成字段直接剔除，值永远送不进函数体。
import asyncio
import base64
import logging
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_conversation_id, current_user_id

logger = logging.getLogger(__name__)


def _collect_file_refs(node, known, referenced) -> None:
    """递归找出 params 里引用了会话文件的字符串，把名字记进 referenced。

    **不改写值**：模型在清单里看到的名字就是沙箱 work_dir 里的名字，命中与否字符串都一样。
    这消掉了旧设计「命中就把 file_id 拼成 <uuid>.xlsx」的误伤面——`out_name: "已脱敏.xlsx"`
    恰好等于上一轮产物的名字，旧逻辑会把它拼成 `已脱敏.xlsx.xlsx`。

    文件由沙箱写进本次执行自己的 work_dir（每次执行一个 uuid 目录），路径经 stdin 交给脚本，
    所以这里只给裸文件名，由脚本 join(work_dir, name) 定位——工具侧不该猜沙箱的目录布局。

    「只认等于本会话文件名的字符串」本身就是参数注入的防线：模型填 `../../etc/passwd`
    只会原样传给脚本，脚本按 basename 拼进 work_dir 后 open() 直接失败，不需要额外校验代码。
    **名字只用于查表，绝不参与路径拼接**：进路径的永远是 store 自己发的 file_id。
    """
    if isinstance(node, str):
        if node in known:
            referenced.add(node)
    elif isinstance(node, list):
        for v in node:
            _collect_file_refs(v, known, referenced)
    elif isinstance(node, dict):
        for v in node.values():
            _collect_file_refs(v, known, referenced)


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

        # 认出 params 里引用了哪些会话文件，把它们的字节收齐；值本身不用改
        known = await file_store.list_by_name(user_id, conversation_id)
        referenced = set()
        _collect_file_refs(params, known, referenced)
        files = []
        for name in sorted(referenced):
            # 用条目自己的 file_id 去读。名字是模型可见的键，不是路径键，直接拿去 read 会被拒
            got = await file_store.read(user_id, conversation_id, known[name]["file_id"])
            if got is None:
                return {"ok": False, "error": f"文件不存在或无权访问：{name}"}
            files.append({"name": name, "data": base64.b64encode(got[1]).decode()})

        # 脚本源码从白名单路径读盘：模型递进来的只有 skill/script 两个名字
        script_source = await asyncio.to_thread(path.read_text, encoding="utf-8")
        result = await sandbox.run(script_source, params, files)

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
