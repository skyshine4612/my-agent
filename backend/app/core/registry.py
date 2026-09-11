# app/core/registry.py
# 工具注册表（ToolRegistry）：统一管理工具元信息，把工具转成 OpenAI function calling schema
# 供 LLM 决策调用，并统一执行同步/异步工具函数。
import inspect
from dataclasses import dataclass
from typing import Callable


def _openai_safe_schema(schema: dict) -> dict:
    """把 pydantic 生成的 schema 收拾成 OpenAI function calling 能吃的形式。

    两处清理：递归去掉 pydantic 塞的 title（冗余且会撑大 prompt）；
    把 Optional 产生的 anyOf:[{type:X},{type:null}] 压平成 {type:X}（OpenAI 对 anyOf 支持有限）。
    """
    def walk(node):
        if isinstance(node, dict):
            node.pop("title", None)
            variants = node.get("anyOf")
            if isinstance(variants, list):
                non_null = [v for v in variants if v.get("type") != "null"]
                if len(non_null) == 1:
                    node.pop("anyOf", None)
                    node.update(walk(non_null[0]))
                    return node
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    schema.pop("title", None)
    # pydantic 把模型 docstring 复制成根节点 description，与工具级 description 文本重复，清掉
    schema.pop("description", None)
    return walk(schema)


@dataclass
class Tool:
    """单个工具的元信息 + 可调用函数。

    属性：
        name:        工具名（LLM 通过它选择要调用的工具）
        description: 工具用途描述，写入 OpenAI schema 供 LLM 理解何时调用
        parameters:  JSON Schema 形式的入参定义，约束工具接受的参数结构
        fn:          真正的可调用对象（支持同步或异步函数）
        label:       中文动作短语（如「查询火车票」），供前端工具气泡展示；空则回退英文名
    """
    name: str
    description: str
    parameters: dict
    fn: Callable
    label: str = ""


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

    def register(self, name, description, parameters, fn, label=""):
        """注册一个工具：用名字作为唯一键，保存元信息与可调用函数。

        label：可选的中文动作短语（如「查询火车票」），供前端工具气泡展示；缺省回退英文名。
        """
        self._tools[name] = Tool(name, description, parameters, fn, label)

    def register_tool(self, tool, label=""):
        """从 langchain tool 对象注册：schema 由 pydantic 生成，执行走 tool.ainvoke。

        存 tool 对象而非底层函数：ainvoke 返回的仍是结构化原始对象（v1 不再包装成
        ToolMessage），截断与 spill 判断拿得到结构；同时白拿 pydantic 校验——
        类型强转、缺必填报错、多余参数静默忽略。
        """
        params = _openai_safe_schema(tool.args_schema.model_json_schema())
        self._tools[tool.name] = Tool(tool.name, tool.description, params, tool, label)

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

    def to_openai_schemas(self, names):
        """把指定名字的工具转成 OpenAI function calling 的 schema 列表。"""
        # 遍历请求的名字，仅保留已注册的工具，逐个转成 {"type":"function","function":{...}} 结构
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.parameters}} for n in names if
                (t := self._tools.get(n))]

    async def call_raw(self, name, arguments):
        """执行工具并返回结构化原始结果（dict/list/str 等），不做 str() 转换。

        供 run_tool 做结构化截断使用：拿到原始结构才能按类型截断（list 前 3 条 / dict 前 10 键），
        而 `str()` 会把结构信息压平丢失。
        """
        t = self._tools[name]
        fn = t.fn
        # langchain tool 对象：走 ainvoke 调用（拿到的仍是结构化原始对象，另白拿 pydantic 校验）
        if hasattr(fn, "ainvoke"):
            return await fn.ainvoke(arguments)
        # 老式普通函数（Task 4 转换完其余工具后这条分支删除）：
        # 解包参数字典调用，拿到原始结果（可能是普通值，也可能是协程）
        res = fn(**arguments)
        # 若结果是可等待对象（协程/异步函数），则 await 取出真实结果
        if inspect.isawaitable(res):
            res = await res
        return res

    async def call(self, name, arguments) -> str:
        """执行工具，支持同步/异步函数，统一返回字符串结果（委托 call_raw 后 str()）。"""
        res = await self.call_raw(name, arguments)
        # 统一转成字符串返回，方便 LLM 后续把观察结果回填进对话
        return str(res)
