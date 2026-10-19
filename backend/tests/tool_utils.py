# tests/tool_utils.py
# 测试辅助：直接调用结构化工具 / 构造可执行的占位工具。
import json
from types import SimpleNamespace

from langchain_core.tools import tool

_ANY_ARGS = {"type": "object", "properties": {}, "additionalProperties": True}


def make_stub_tool(name, run):
    """可执行的占位工具：宽松 args_schema 接收任意参数，执行时委托给 run(name, args)。"""
    @tool(name, args_schema=_ANY_ARGS, response_format="content_and_artifact")
    async def _stub(**kwargs):
        """占位工具。"""
        raw = await run(name, kwargs)
        content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        return content, raw

    return SimpleNamespace(fn=_stub)


async def ainvoke_raw(tool, args):
    result = await tool.ainvoke({
        "type": "tool_call", "id": "test", "name": tool.name, "args": args,
    })
    artifact = getattr(result, "artifact", None)
    return artifact if artifact is not None else result.content
