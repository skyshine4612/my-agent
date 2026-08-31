# datasource/http_base.py
"""HTTP 数据源基类：封装 httpx 异步客户端与统一关闭。"""
import httpx


class HttpDataSource:
    """HTTP 数据源基类：持有共享的 httpx.AsyncClient，子类只需实现具体接口与认证。"""

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()
