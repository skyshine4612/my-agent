# app/core/llm.py
# LLM 客户端模块：定义统一的 LLM 调用接口，并提供百炼 Qwen 的 OpenAI 兼容实现与单例工厂
from abc import ABC, abstractmethod

from openai import AsyncOpenAI

from app.config import settings


class LLMClient(ABC):
    """LLM 客户端抽象接口：所有 LLM 实现（真实或 Fake）都需继承并实现以下两个方法"""

    @abstractmethod
    async def chat(self, messages, tools=None, response_format=None) -> dict:
        """发起一次对话调用。

        返回 dict，格式为：
            {"content": str, "tool_calls": list | None}
        其中 tool_calls 为模型发起的工具调用列表，无工具调用时为 None。
        response_format 可选，传入如 {"type":"json_object"} 时强制模型输出合法 JSON。
        """
        ...

    @abstractmethod
    async def complete(self, messages) -> str:
        """发起一次纯文本补全调用（供摘要/记忆提炼等场景使用），返回纯文本字符串。"""
        ...


class OpenAICompatLLM(LLMClient):
    """OpenAI 兼容协议 LLM 实现：通过 openai 库的 AsyncOpenAI 调用任意 OpenAI 兼容端点（如百炼 DashScope）"""

    def __init__(self, base_url, api_key, model):
        # 保存模型名，并基于 base_url / api_key 构建异步客户端
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def chat(self, messages, tools=None, response_format=None):
        # 组装请求参数：模型名 + 消息列表，仅在提供 tools 时附带工具定义
        kw = {"model": self.model, "messages": messages}
        if tools:
            kw["tools"] = tools
        # 提供 response_format（如 {"type":"json_object"}）时，要求模型强制输出合法 JSON
        if response_format:
            kw["response_format"] = response_format
        # 调用 Chat Completions 接口，获取首个候选回复
        resp = await self.client.chat.completions.create(**kw)
        msg = resp.choices[0].message
        # 将 pydantic 的工具调用对象序列化为 dict（model_dump），便于上层处理
        return {"content": msg.content or "",
                "tool_calls": [tc.model_dump() for tc in (msg.tool_calls or [])]}

    async def complete(self, messages):
        # 纯文本补全：复用 chat()，仅返回其中的文本内容
        r = await self.chat(messages)
        return r["content"]


class FallbackLLM(LLMClient):
    """无 API Key 时的兜底 LLM：直接返回空结果。

    用于未配置 llm_api_key 时保证服务仍可启动（/api/health、路由挂载正常）；
    此时真实对话会得到空内容，配置 key 后重启即可恢复正常对话。
    """

    async def chat(self, messages, tools=None, response_format=None) -> dict:
        # 无凭证时不做真实调用，返回空的 content 与 tool_calls
        return {"content": "", "tool_calls": None}

    async def complete(self, messages) -> str:
        # 纯文本补全同样返回空字符串
        return ""


# 进程级单例缓存，避免重复创建客户端
_llm = None


def get_llm() -> LLMClient:
    """单例工厂：返回全局唯一的 LLM 客户端实例（基于 settings 中的百炼配置构建）。

    未配置 API Key 时返回 FallbackLLM，避免 AsyncOpenAI 因缺凭证在启动阶段抛异常，
    保证服务在无真实 key 时也能正常启动。
    """
    global _llm
    if _llm is None:
        # 有 key 才构建真实客户端；否则用兜底实现保证服务可启动
        if settings.llm_api_key:
            _llm = OpenAICompatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
        else:
            _llm = FallbackLLM()
    return _llm
