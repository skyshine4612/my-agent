# app/core/db.py
# asyncpg 连接池 + RLS 事务上下文。所有业务查询都必须经由 rls_conn 执行，
# 否则连接以 postgres 身份运行（BYPASSRLS），数据库里的策略形同虚设。
import json
import logging
from contextlib import asynccontextmanager

import asyncpg

logger = logging.getLogger(__name__)

# 进程级连接池：由 lifespan 在应用启动时初始化、退出时关闭
_pool: asyncpg.Pool | None = None


async def init_pool(dsn: str) -> None:
    """初始化连接池（应用启动时调用一次）。"""
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    logger.info("[db] 连接池已初始化")


async def close_pool() -> None:
    """关闭连接池（应用退出时调用）。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("[db] 连接池已关闭")


@asynccontextmanager
async def admin_conn():
    """不切角色的裸连接：仅供测试基础设施（清表、造用户）使用。

    业务查询一律走 rls_conn——这里以 postgres 身份运行，RLS 策略完全不生效。
    """
    if _pool is None:
        raise RuntimeError("连接池未初始化：请先 await init_pool(dsn)")
    async with _pool.acquire() as conn:
        yield conn


@asynccontextmanager
async def rls_conn(user_id: str, role: str = "authenticated"):
    """按当前用户开一个事务：切角色 + 注入 JWT claims，让 RLS 策略真正生效。

    set_config(..., true) 是事务局部设置，commit/rollback 后自动失效，
    因此连接归还池中不会把上一个用户的身份带给下一个请求。
    """
    if _pool is None:
        raise RuntimeError("连接池未初始化：请先在 lifespan 里 await init_pool(dsn)")
    async with _pool.acquire() as conn:
        async with conn.transaction():
            # auth.uid() 内部读 request.jwt.claims->>'sub'，所以 JSON 结构必须带 sub 键
            await conn.execute(
                "select set_config('request.jwt.claims', $1, true)",
                json.dumps({"sub": user_id, "role": role}),
            )
            await conn.execute("select set_config('role', $1, true)", role)
            yield conn
