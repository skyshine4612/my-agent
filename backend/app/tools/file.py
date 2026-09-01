# app/tools/file.py
# 文件读取工具：read_file（按 offset 读 + 进度提示）+ grep（正则搜 + offset/context），
# 对齐 Claude Code 的读文件形式。path 都经路径白名单校验，只读当前用户目录下的结果文件。
from app.core.context import current_user_id

# read_file 工具的 OpenAI function-calling schema
READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取某个工具结果文件的片段。当工具结果因过长只显示了预览、末尾有文件路径提示时，用此工具按 offset 读回被省略的部分。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "结果文件的相对路径（来自工具结果里的路径提示）"},
                "offset": {"type": "integer", "description": "起始字符偏移，默认 0"},
                "limit": {"type": "integer", "description": "读取字符数，默认 3500"},
            },
            "required": ["path"],
        },
    },
}

# grep 工具的 OpenAI function-calling schema
GREP_TOOL = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": "在某个工具结果文件里正则搜索关键词，返回每个命中的位置与上下文片段。当需要定位结果里的某个车次号、价格、名称等关键信息时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式，如 MU5160、525元"},
                "path": {"type": "string", "description": "结果文件的相对路径"},
            },
            "required": ["pattern", "path"],
        },
    },
}


def make_read_file(store):
    """构造 read_file 工具：闭包 ToolResultStore，按 path + offset 读片段，按 user_id 隔离。"""

    async def read_file(path, offset=0, limit=3500):
        """按路径与字符偏移读取结果文件的一段；未读完时末尾带进度提示，供继续读。"""
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

    async def grep(pattern, path):
        """在结果文件里正则搜索，返回每个命中的匹配内容、字符位置与上下文片段。"""
        return await store.search(path, pattern, user_id=current_user_id.get())

    return grep


def register_file_tools(registry, tool_result_store):
    """把 read_file / grep 工具注入 ToolRegistry（需要 tool_result_store，故在 service 层装配时注册）。"""
    rf = make_read_file(tool_result_store)
    rf.__name__ = "read_file"
    rf.description = READ_FILE_TOOL["function"]["description"]
    rf.parameters = READ_FILE_TOOL["function"]["parameters"]
    registry.register("read_file", rf.description, rf.parameters, rf, "读取结果文件")

    gr = make_grep(tool_result_store)
    gr.__name__ = "grep"
    gr.description = GREP_TOOL["function"]["description"]
    gr.parameters = GREP_TOOL["function"]["parameters"]
    registry.register("grep", gr.description, gr.parameters, gr, "搜索结果文件")
