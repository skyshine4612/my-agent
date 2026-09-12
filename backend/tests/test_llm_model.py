# tests/test_llm_model.py
# langchain 模型适配层测试：验证 get_chat_model() 的两个分支，不依赖真实 LLM / 网络
import httpx
import pytest
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from app.config import settings
from app.core import llm


@tool
def _dummy_tool(x: str) -> str:
    """测试用占位工具：仅为让 create_agent 装配工具集。"""
    return x


def _reset(monkeypatch):
    """清掉进程级单例缓存，让下一次 get_chat_model() 按当前 settings 重新构建。"""
    monkeypatch.setattr(llm, "_chat_model", None)


@pytest.mark.asyncio
async def test_get_chat_model_without_key_drives_create_agent(monkeypatch):
    """无 key：假模型能被 create_agent 接受并跑完一轮（bind_tools 在 invoke 阶段才触发）"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    _reset(monkeypatch)

    agent = create_agent(llm.get_chat_model(), tools=[_dummy_tool])
    result = await agent.ainvoke({"messages": [HumanMessage("你好")]})
    # 无 key 时不产出内容：服务能起、对话返回空
    assert result["messages"][-1].content == ""


@pytest.mark.asyncio
async def test_fake_model_returns_empty_content(monkeypatch):
    """无 key：假模型重复调用均返回空内容（内层迭代器不会耗尽）"""
    monkeypatch.setattr(settings, "llm_api_key", "")
    _reset(monkeypatch)

    model = llm.get_chat_model()
    for _ in range(3):
        assert (await model.ainvoke([HumanMessage("你好")])).content == ""


def test_get_chat_model_with_key_returns_langchain_model(monkeypatch):
    """有 key：返回 ChatOpenAI，且构建阶段不发起网络请求（base_url 不可达也能建出来）"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1")
    _reset(monkeypatch)

    model = llm.get_chat_model()
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == settings.llm_model


def test_enable_thinking_passed_via_extra_body(monkeypatch):
    """enable_thinking 经 extra_body 透传（Qwen 专有字段，走 model_kwargs 会被 openai SDK 拒收）"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "enable_thinking", True)
    _reset(monkeypatch)

    assert llm.get_chat_model().extra_body == {"enable_thinking": True}


def test_get_chat_model_is_singleton(monkeypatch):
    """单例：重复调用返回同一实例，避免每请求重建 httpx 连接池"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    _reset(monkeypatch)

    assert llm.get_chat_model() is llm.get_chat_model()


def test_timeout_passed_explicitly(monkeypatch):
    """timeout 显式对齐 120s：不写会落到 openai SDK 默认的 600s，改变蒸馏超时降级的触发时机"""
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    _reset(monkeypatch)

    assert llm.get_chat_model().request_timeout == httpx.Timeout(120.0, connect=10.0)


@pytest.mark.asyncio
async def test_close_chat_model_closes_client_and_noop_without_key(monkeypatch):
    """close_chat_model 关闭有 key 分支的连接池；无 key 的假模型没有底层客户端，是安全 no-op。"""
    # 有 key：base_url 不可达也能构建（构建阶段不发请求），关闭后底层客户端确实已关
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", "http://127.0.0.1:1/v1")
    _reset(monkeypatch)
    client = llm.get_chat_model().root_async_client

    await llm.close_chat_model()
    assert client.is_closed()

    # 无 key：假模型无底层客户端，直接返回
    monkeypatch.setattr(settings, "llm_api_key", "")
    _reset(monkeypatch)
    llm.get_chat_model()
    await llm.close_chat_model()
