# app/core/sandbox.py
# 沙箱客户端：向后端暴露「跑一个已评审脚本」这一件事。
# 双端超时：沙箱内有看门狗杀脚本，后端 httpx 等的是沙箱超时 + 5s —— 这 5 秒是刻意的，
# 让沙箱有机会把「超时了」作为结构化响应返回，而不是让后端只看到连接断开。
import logging

import httpx

logger = logging.getLogger(__name__)

# 后端比沙箱多等的秒数
_TIMEOUT_MARGIN = 5

# 沙箱不可达时统一的失败形状：调用方（工具层）只需看 exit_code，不必区分「脚本失败」与「沙箱挂了」
_UNREACHABLE = {"exit_code": -1, "stdout": "", "stderr": "", "artifacts": [], "duration_ms": 0}


class SandboxClient:
    """沙箱 HTTP 客户端：长连接复用，生命周期与其余数据源一致（启动时装配一次、进程退出时关闭）。"""

    def __init__(self, base_url, default_timeout, transport=None):
        self.base_url = base_url.rstrip("/")
        self.default_timeout = default_timeout
        # transport 只为测试注入假沙箱；生产传 None 走真实网络
        self._client = httpx.AsyncClient(base_url=self.base_url,
                                         timeout=httpx.Timeout(default_timeout + _TIMEOUT_MARGIN,
                                                               connect=10.0),
                                         transport=transport)

    async def run(self, script, args, files, timeout=None):
        """跑一个脚本，返回 {exit_code, stdout, stderr, artifacts, duration_ms}。"""
        payload = {"script": script, "args": args, "files": files,
                   "timeout": int(timeout if timeout is not None else self.default_timeout)}
        try:
            resp = await self._client.post("/run", json=payload)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("[沙箱] 调用失败：%s", e)
            return {**_UNREACHABLE, "stderr": f"沙箱不可达：{e}"}

    async def health(self):
        """探活：容器编排与排障用，失败不抛异常。"""
        try:
            resp = await self._client.get("/health")
            return resp.status_code == 200 and bool(resp.json().get("ok"))
        except Exception:
            return False

    async def close(self):
        """关闭连接池（进程退出前调用，与其余数据源一起收口）。"""
        await self._client.aclose()
