# app/core/registry.py
# 工具注册表（ToolRegistry）：统一管理工具元信息并执行工具函数。模型侧的 schema 由 langchain
# 在装配时从各 tool 对象的 pydantic args_schema 生成，本模块不经手。
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
    """工具注册表：统一管理工具并执行。

    职责：
        - register_tool：注册 langchain tool 对象
        - call_raw：按名字执行工具并返回结构化原始结果
    """

    def __init__(self):
        # 内部字典：工具名 → Tool 对象
        self._tools = {}

    def register_tool(self, tool, label=""):
        """从 langchain tool 对象注册，执行走 tool.ainvoke。

        存 tool 对象而非底层函数：ainvoke 返回结构化原始对象，截断与 spill 判断拿得到结构；
        同时白拿 pydantic 校验——类型强转、缺必填报错、多余参数静默忽略。
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
        """执行工具并返回结构化原始结果（dict/list/str 等），不做 str() 转换。

        调用方拿到原始结构才能按类型截断（list 前 20 条 / dict 前 20 键），
        `str()` 会把结构信息压平丢失。
        """
        return await self._tools[name].fn.ainvoke(arguments)
