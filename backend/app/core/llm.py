# app/core/llm.py
# LLM 接入层：全项目唯一的 LLM 通路 —— get_chat_model() 返回的 langchain 模型。
import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from app.config import settings


class _EmptyFakeChatModel(GenericFakeChatModel):
    """无凭证时的假 langchain 模型：每次调用都产出空内容消息。

    GenericFakeChatModel.bind_tools 直接 raise NotImplementedError，而 create_agent 驱动模型前
    会调用 bind_tools（调用期而非装配期），故覆盖为原样返回自身。
    """

    def bind_tools(self, tools, **kwargs):
        return self


def _empty_messages():
    """无限产出空内容 AIMessage 的迭代器：每次都是新对象，避免跨调用共享同一实例。"""
    while True:
        yield AIMessage(content="")


# 进程级单例缓存：ChatOpenAI 持有 httpx 连接池，每请求重建会泄漏连接
_chat_model: BaseChatModel | None = None


def get_chat_model() -> BaseChatModel:
    """返回 langchain 模型实例，供 create_agent 构建子图/主图。

    走 DashScope 的 OpenAI 兼容协议；无 key 时返回假模型，保证服务仍可启动。
    """
    global _chat_model
    if _chat_model is None:
        if settings.llm_api_key:
            # enable_thinking 是 Qwen 专有字段，需经 extra_body 合并到请求体顶层（model_kwargs 会被 SDK 拒收）；
            # timeout 显式给出：不写会落到 SDK 默认的 600s
            _chat_model = ChatOpenAI(
                model=settings.llm_model,
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                temperature=settings.llm_temperature,
                timeout=httpx.Timeout(120.0, connect=10.0),
                extra_body={"enable_thinking": settings.enable_thinking},
            )
        else:
            _chat_model = _EmptyFakeChatModel(messages=_empty_messages())
    return _chat_model


async def close_chat_model():
    """关闭单例模型持有的 httpx 连接池（进程退出前由 lifespan 调用）。

    无 key 的假模型没有底层客户端，直接返回。
    """
    client = getattr(_chat_model, "root_async_client", None)
    if client is not None:
        await client.close()
