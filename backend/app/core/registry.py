# app/core/registry.py
# 工具注册表（ToolRegistry）：统一管理工具元信息；生产执行由 create_agent 的 ToolNode 负责。
# 模型侧的 schema 由 langchain 在装配时从各 tool 对象的 pydantic args_schema 生成，本模块不经手。
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    """单个工具的条目：可调用函数 + 前端展示短语。

    工具名由 `_tools` 的字典键承载；description 与参数定义在 langchain tool 对象上，
    模型侧的 schema 由框架直接从那里生成。
    """

    fn: Callable
    # 中文动作短语（如「查询火车票」），供前端工具气泡展示；空则回退英文名
    label: str = ""


class ToolRegistry:
    """工具注册表：统一管理工具元信息。

    职责：
        - register_tool：注册 langchain tool 对象
        - get / list_names / label：供 create_agent 装配与前端展示
        - call_raw：仅供测试/直接调用，不在生产执行路径上
    """

    def __init__(self):
        # 内部字典：工具名 → Tool 对象
        self._tools = {}

    def register_tool(self, tool, label=""):
        """从 langchain tool 对象注册；工具对象交给 create_agent / ToolNode 执行。

        存 tool 对象而非底层函数：schema 由框架从 args_schema 生成并完成 pydantic 校验；
        结构化工具通过 content_and_artifact 把原始结构带在 ToolMessage.artifact 上。
        """
        self._tools[tool.name] = Tool(tool, label)

    def get(self, name):
        """按名字取回工具对象。"""
        return self._tools[name]

    def list_names(self):
        """列出当前已注册的所有工具名。"""
        return list(self._tools.keys())

    def label(self, name):
        """返回工具的中文动作短语，未设 label 或未注册时回退英文名。"""
        t = self._tools.get(name)
        return (t.label or name) if t else name

    async def call_raw(self, name, arguments):
        """直接执行工具并返回原始结果；不在生产执行路径上，仅供测试/直接调用。

        结构化工具（content_and_artifact）必须用合成 ToolCall 调用才能取回 artifact；
        普通 dict 调用会只返回 content，artifact 被静默丢弃。
        """
        tool = self._tools[name].fn
        if getattr(tool, "response_format", None) == "content_and_artifact":
            msg = await tool.ainvoke({"type": "tool_call", "id": "call_raw",
                                      "name": name, "args": arguments})
            return msg.artifact if msg.artifact is not None else msg.content
        return await tool.ainvoke(arguments)
