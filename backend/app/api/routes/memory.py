# app/api/routes/memory.py
# 记忆查询路由：GET /api/memory/long-term、GET /api/memory/short-term/{conversation_id}，
# 供前端「记忆」页展示长期偏好与某会话的短期记忆（摘要 + 最近几轮对话）；均按 X-User-Id 做用户隔离。
from fastapi import APIRouter, Header

from app.services.agent_service import service

router = APIRouter()


@router.get("/memory/long-term")
async def get_long_term_memory(x_user_id: str = Header(default="anonymous")):
    """返回当前用户的长期记忆（跨会话偏好）列表，按 importance 降序。"""
    return await service.long_term_memory.get_all(x_user_id)


@router.get("/memory/short-term/{conversation_id}")
async def get_short_term_memory(conversation_id: str, x_user_id: str = Header(default="anonymous")):
    """返回某会话的短期记忆（summary 摘要 + 最近几轮对话原文）；会话归属校验失败返回空列表。"""
    # 用户隔离：会话必须归属当前用户，防止跨用户读取他人会话的短期记忆
    if not await service.conversation_store.belongs_to(x_user_id, conversation_id):
        return {"records": []}
    records = await service.short_term_memory.get_records(conversation_id)
    return {"records": records}
