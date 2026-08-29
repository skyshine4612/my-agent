# app/core/registry.py
# 工具注册表（ToolRegistry）与 Agent 注册表（AgentRegistry）：
# ToolRegistry 统一管理工具元信息，把工具转成 OpenAI function calling schema 供 LLM 决策调用，
# 并统一执行同步/异步工具函数；AgentRegistry 管理子 Agent，供 Planner 生成能力清单、Orchestrator 按名调度。
import inspect
from dataclasses import dataclass
from typing import Callable


@dataclass
class Tool:
    """单个工具的元信息 + 可调用函数。

    属性：
        name:        工具名（LLM 通过它选择要调用的工具）
        description: 工具用途描述，写入 OpenAI schema 供 LLM 理解何时调用
        parameters:  JSON Schema 形式的入参定义，约束工具接受的参数结构
        fn:          真正的可调用对象（支持同步或异步函数）
    """
    name: str
    description: str
    parameters: dict
    fn: Callable


class ToolRegistry:
    """工具注册表：统一管理工具，转成 OpenAI function schema 供 LLM 调用。

    职责：
        - register：注册工具（名字 → Tool 元信息 + 函数）
        - to_openai_schemas：把指定名字的工具转成 OpenAI function calling schema 列表
        - call：按名字执行工具，兼容同步/异步函数，统一返回字符串
    """
    def __init__(self):
        # 内部字典：工具名 → Tool 对象
        self._tools = {}

    def register(self, name, description, parameters, fn):
        """注册一个工具：用名字作为唯一键，保存元信息与可调用函数。"""
        self._tools[name] = Tool(name, description, parameters, fn)

    def get(self, name):
        """按名字取回工具对象。"""
        return self._tools[name]

    def list_names(self):
        """列出当前已注册的所有工具名。"""
        return list(self._tools.keys())

    def to_openai_schemas(self, names):
        """把指定名字的工具转成 OpenAI function calling 的 schema 列表。"""
        # 遍历请求的名字，仅保留已注册的工具，逐个转成 {"type":"function","function":{...}} 结构
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                "parameters": t.parameters}} for n in names if (t := self._tools.get(n))]

    async def call(self, name, arguments) -> str:
        """执行工具，支持同步/异步函数，统一返回字符串结果。"""
        # 按名字取出工具对象
        t = self._tools[name]
        # 解包参数字典调用工具函数，拿到原始结果（可能是普通值，也可能是协程）
        res = t.fn(**arguments)
        # 若结果是可等待对象（协程/异步函数），则 await 取出真实结果
        if inspect.isawaitable(res):
            res = await res
        # 统一转成字符串返回，方便 LLM 后续把观察结果回填进对话
        return str(res)


class AgentRegistry:
    """Agent 注册表：管理子 Agent，供 Planner 生成能力清单、Orchestrator 按名调度。

    职责：
        - register：注册 Agent（名字 → Agent 对象）
        - get / list_names：按名取回、列出全部 Agent 名
        - describe：生成能力清单文本，注入 Planner 的 prompt
    """
    def __init__(self):
        # 内部字典：Agent 名 → Agent 对象
        self._agents = {}

    def register(self, agent):
        """注册一个 Agent：以 Agent 的 name 字段作为唯一键。"""
        self._agents[agent.name] = agent

    def get(self, name):
        """按名字取回 Agent 对象。"""
        return self._agents[name]

    def list_names(self):
        """列出当前已注册的所有 Agent 名。"""
        return list(self._agents.keys())

    def describe(self):
        """生成能力清单文本（name: system_prompt），供 Planner 的 prompt 注入。"""
        # 每个 Agent 生成一行「- 名称: system_prompt」，用换行拼接成清单文本
        return "\n".join(f"- {a.name}: {a.system_prompt}" for a in self._agents.values())
