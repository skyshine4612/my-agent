# datasource/mcp_base.py
"""MCP 数据源基类：封装 ModelScope 托管 MCP（streamable HTTP）的连接管理。

把「懒加载持久 session + 统一工具调用 + 关闭清理」抽成基类，供高德/12306/机票等
多个 MCP 数据源复用；子类只需实现具体的查询方法，连接管理由基类统一处理。
"""
import asyncio
import json
import logging
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client, create_mcp_http_client

logger = logging.getLogger(__name__)


class McpDataSource:
    """MCP 数据源基类：懒加载持久 session + 统一工具调用 + 关闭清理。"""

    def __init__(self, url, token=None, headers=None):
        # 连接参数：MCP 服务地址 + 认证头。
        # 默认用 ModelScope 令牌（Authorization: Bearer）；子类也可传自定义 headers（如 variflight 官方的 X-API-Key）。
        self.url = url
        self.headers = headers if headers is not None else {"Authorization": f"Bearer {token}"}
        # streamable HTTP 协议：headers 需通过 http_client 传入
        self.http_client = create_mcp_http_client(headers=self.headers)
        # 懒加载的持久 session：首次调用建立连接，后续复用（省去重复握手）
        self._session = None
        self._exit_stack = None
        self._lock = asyncio.Lock()

    async def _ensure_session(self):
        """懒加载持久 session：首次调用建立连接+initialize，并发安全，失效时重建。"""
        async with self._lock:
            if self._session is not None:
                return self._session
            self._exit_stack = AsyncExitStack()
            read, write = await self._exit_stack.enter_async_context(
                streamable_http_client(self.url, http_client=self.http_client))
            self._session = await self._exit_stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
            return self._session

    async def _call_tool(self, tool_name, arguments):
        """调用 MCP 工具，返回 CallToolResult。连接失效时清理缓存、下次重建。

        加 asyncio.wait_for 超时：ModelScope 限流时连接可能挂起（不返回也不抛异常），
        超时抛 TimeoutError 由上层（run_stream 的 try/except）兜底，避免整个请求卡死。
        """
        session = await self._ensure_session()
        try:
            return await asyncio.wait_for(session.call_tool(tool_name, arguments), timeout=30.0)
        except asyncio.CancelledError as e:
            # MCP 连接中途被任务组取消（如服务器断连），不是上层主动取消本次请求；
            # 转成普通异常，让 run_stream 的工具兜底当「执行失败」处理，避免 CancelledError
            # 一路穿透 except Exception 把整个 SSE 流炸掉。
            await self._discard_session()
            raise ConnectionError(f"MCP 工具 {tool_name} 调用被中断（服务器断连）") from e
        except Exception:
            await self._discard_session()
            raise

    async def _discard_session(self):
        """丢弃失效的 session 与连接；吞掉 aclose 抛出的取消/异常组（避免二次异常掩盖原错误）。"""
        if self._exit_stack is not None:
            try:
                await self._exit_stack.aclose()
            except BaseException:
                pass
            self._exit_stack = None
            self._session = None

    @staticmethod
    def _extract_text(result) -> str:
        """从 CallToolResult 提取文本内容（优先 structured_content，兜底拼 content 文本块）。"""
        sc = getattr(result, "structured_content", None)
        if sc is not None:
            return sc if isinstance(sc, str) else json.dumps(sc, ensure_ascii=False)
        text = "".join(getattr(b, "text", "") for b in getattr(result, "content", []))
        return text

    async def close(self):
        """关闭持久 session（进程退出前调用；anyio 跨 task 退出会报错，静默吞掉）。"""
        await self._discard_session()
