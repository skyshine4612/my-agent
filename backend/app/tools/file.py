# app/tools/file.py
# 文件读取工具：read_file（按 offset 读 + 进度提示）+ grep（正则搜 + offset/context），
# 对齐 Claude Code 的读文件形式。path 都经路径白名单校验，只读当前用户目录下的结果文件。
# 两个工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
# 参数描述用 Annotated[T, Field(description=...)] 表达：它是模型可见契约的一部分，
# 裸注解会静默丢掉参数级描述，等于调用行为回归。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_conversation_id, current_user_id
from app.core.prompts import render_file_directory
from app.tools.content import render_content

# 清单列多少个文件：够覆盖一个会话的常态用量，又不至于把上下文塞满。
# 必须 ≤ FileStore 反查用的 FILE_REF_LIMIT，否则会出现「清单里看得见、引用时解析不到」的死角。
LIST_LIMIT = 20


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

    @tool(response_format="content_and_artifact")
    async def grep(
        pattern: Annotated[str, Field(description="正则表达式，如 MU5160、525元")],
        path: Annotated[str, Field(description="结果文件的相对路径")],
    ) -> tuple[str, dict]:
        """在某个工具结果文件里正则搜索关键词，返回每个命中的位置与上下文片段。当需要定位结果里的某个车次号、价格、名称等关键信息时使用。"""
        raw = await store.search(path, pattern, user_id=current_user_id.get())
        return render_content(raw), raw

    return grep


def make_list_uploaded_files(file_store):
    """构造 list_uploaded_files 工具：闭包 FileStore，列本会话的文件（上传件 + 脚本产物）。

    这个清单以前是每轮塞进 system prompt 的，改成按需查：清单只对「用户指的是哪个文件」
    有用，而用户点名的那一轮，文件名就在消息正文里（render_user_message 折的）。
    """

    @tool
    async def list_uploaded_files() -> str:
        """列出本会话里可用的文件（用户上传的 + 之前生成的产物，含文件名与大小）。当不确定会话里有哪些文件，或用户提到的是之前轮次提到的文件时使用。"""
        entries = await file_store.list_by_name(current_user_id.get(),
                                                current_conversation_id.get(),
                                                limit=LIST_LIMIT)
        return render_file_directory(entries) or "本会话还没有任何文件。"

    return list_uploaded_files


def register_file_tools(registry, tool_result_store):
    """把 read_file / grep 工具注入 ToolRegistry（需要 tool_result_store，故在 service 层装配时注册）。"""
    registry.register_tool(make_read_file(tool_result_store), "读取结果文件")
    registry.register_tool(make_grep(tool_result_store), "搜索结果文件")


def register_uploaded_files_tool(registry, file_store):
    """把 list_uploaded_files 注入 ToolRegistry（需要 FileStore，与上面两个读的 store 不是同一个）。"""
    registry.register_tool(make_list_uploaded_files(file_store), "列出会话文件")
