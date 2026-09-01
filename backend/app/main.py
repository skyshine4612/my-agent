# main.py
# FastAPI 应用入口：创建应用实例、注册中间件与路由
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import chat, conversation, memory
from app.core.logging import setup_logging
from app.services.agent_service import service

# 应用启动时初始化日志系统（控制台 + 文件 logs/app.log）
setup_logging()


@asynccontextmanager
async def lifespan(app):
    """应用生命周期：service 是模块级单例（import 时已预加载所有数据源与 LLM 客户端），
    启动阶段无需额外初始化；关闭阶段统一优雅关闭所有长连接（httpx / MCP session）。"""
    yield
    await service.close()


# 创建 FastAPI 应用实例
app = FastAPI(title="可扩展业务 Agent 平台", lifespan=lifespan)

# 挂载业务路由：SSE 流式对话 + 会话管理 + 记忆查询（统一挂到 /api 前缀下）
app.include_router(chat.router, prefix="/api")
app.include_router(conversation.router, prefix="/api")
app.include_router(memory.router, prefix="/api")


@app.get("/api/health")
def health():
    """健康检查端点：返回固定 JSON，供服务探活/验证使用"""
    return {"status": "ok"}
