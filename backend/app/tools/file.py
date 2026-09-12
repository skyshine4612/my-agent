# app/tools/file.py
# 文件读取工具：read_file（按 offset 读 + 进度提示）+ grep（正则搜 + offset/context），
# 对齐 Claude Code 的读文件形式。path 都经路径白名单校验，只读当前用户目录下的结果文件。
# 两个工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
# 参数描述用 Annotated[T, Field(description=...)] 表达：它是模型可见契约的一部分，
# 裸注解会静默丢掉参数级描述，等于调用行为回归。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_user_id


def make_read_file(store):
    """构造 read_file 工具：闭包 ToolResultStore，按 path + offset 读片段，按 user_id 隔离。"""

    @tool
    async def read_file(
        path: Annotated[str, Field(description="结果文件的相对路径（来自工具结果里的路径提示）")],
        offset: Annotated[int, Field(description="起始字符偏移，默认 0")] = 0,
        limit: Annotated[int, Field(description="读取字符数，默认 3500")] = 3500,
    ) -> str:
        """读取某个工具结果文件的片段。当工具结果因过长只显示了预览、末尾有文件路径提示时，用此工具按 offset 读回被省略的部分。"""
        seg = await store.read(path, offset, limit, user_id=current_user_id.get())
        if seg is None:
            return "未找到该文件或无权访问"
        content = seg["content"]
        if seg["eof"]:
            return content
        return (f"{content}\n[未读完：已读 {seg['next_offset']}/{seg['total']} 字符，"
                f"继续 read_file(offset={seg['next_offset']})]")

    return read_file


def make_grep(store):
    """构造 grep 工具：闭包 ToolResultStore，按 path + 正则搜，返回位置与上下文片段。"""

    @tool
    async def grep(
        pattern: Annotated[str, Field(description="正则表达式，如 MU5160、525元")],
        path: Annotated[str, Field(description="结果文件的相对路径")],
    ) -> dict:
        """在某个工具结果文件里正则搜索关键词，返回每个命中的位置与上下文片段。当需要定位结果里的某个车次号、价格、名称等关键信息时使用。"""
        return await store.search(path, pattern, user_id=current_user_id.get())

    return grep


def register_file_tools(registry, tool_result_store):
    """把 read_file / grep 工具注入 ToolRegistry（需要 tool_result_store，故在 service 层装配时注册）。"""
    registry.register_tool(make_read_file(tool_result_store), "读取结果文件")
    registry.register_tool(make_grep(tool_result_store), "搜索结果文件")
