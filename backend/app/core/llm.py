# app/core/llm.py
# LLM 客户端模块：定义统一的 LLM 调用接口，并提供百炼 Qwen 的 OpenAI 兼容实现与单例工厂
from abc import ABC, abstractmethod
from typing import AsyncIterator

import httpx
from openai import AsyncOpenAI

from app.config import settings


class LLMClient(ABC):
    """LLM 客户端抽象接口：所有 LLM 实现（真实或 Fake）都需继承并实现以下三个方法"""

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

    @abstractmethod
    async def stream_chat(self, messages, tools=None) -> AsyncIterator[dict]:
        """以流式方式发起一次对话调用。

        返回异步迭代器，逐次产出事件 dict：
            {"type": "content", "text": <增量文本>}
            {"type": "end", "tool_calls": [...] 或 None}
        其中 content 事件为 delta 文本增量（可即时推给前端），end 事件为收尾信号，
        tool_calls 为按 index 合并后的工具调用列表，形状与 chat() 一致；无工具调用时为 None。
        """
        ...


class OpenAICompatLLM(LLMClient):
    """OpenAI 兼容协议 LLM 实现：通过 openai 库的 AsyncOpenAI 调用任意 OpenAI 兼容端点（如百炼 DashScope）"""

    def __init__(self, base_url, api_key, model):
        # 保存模型名，并基于 base_url / api_key 构建异步客户端
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=httpx.Timeout(120.0, connect=10.0))

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

    async def stream_chat(self, messages, tools=None):
        """以流式方式发起对话，逐事件产出文本增量与收尾工具调用。

        产出事件：
            {"type": "content", "text": <增量文本>}          —— 文本 delta，可即时推给前端
            {"type": "end", "tool_calls": [...] | None}      —— 收尾信号，按 index 合并后的工具调用
        工具调用分片按 index 归并 id/name/arguments（arguments 跨分片累积拼接），
        最后按 index 升序组装成与 chat() 同形状的 tool_calls（无工具调用时为 None）。
        """
        # 组装请求参数，stream=True 开启 SSE 流式返回
        kw = {"model": self.model, "messages": messages, "stream": True}
        if tools:
            kw["tools"] = tools
        stream = await self.client.chat.completions.create(**kw)
        # 流式工具调用是分片到达的：用 index 作为 key 归并每个分片的 id / name / arguments
        acc = {}
        async for chunk in stream:
            # 空 choices（某些提供商的 keep-alive 心跳）直接跳过
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            # 文本增量：逐段产出，让上层能即时把 token 推给前端
            if delta.content:
                yield {"type": "content", "text": delta.content}
            for tc in (delta.tool_calls or []):
                idx = tc.index
                if idx not in acc:
                    # 首个分片初始化结构；arguments 会跨多个分片累积拼接。
                    # 补 "type":"function" 与 chat() 里 model_dump 的形状对齐，
                    # 避免回传 API 时被严格端点因缺字段拒收。
                    acc[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                entry = acc[idx]
                # id 通常在首个分片出现一次，后续分片为空，仅在非空时覆盖
                if tc.id:
                    entry["id"] = tc.id
                if tc.function:
                    # name / arguments 都可能分片到达，需累积拼接而非覆盖
                    if tc.function.name:
                        entry["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        entry["function"]["arguments"] += tc.function.arguments
        # 按 index 升序组装成有序工具调用列表（无工具调用时为 None），与 chat() 的 tool_calls 形状一致
        tool_calls = [acc[k] for k in sorted(acc)] or None
        yield {"type": "end", "tool_calls": tool_calls}


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

    async def stream_chat(self, messages, tools=None):
        """无凭证时不做真实调用：不产出任何内容，直接发出 end 收尾事件。"""
        yield {"type": "end", "tool_calls": None}


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
