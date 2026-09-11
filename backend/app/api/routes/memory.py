# app/api/routes/memory.py
# 记忆查询路由：GET /api/memory/long-term、GET /api/memory/short-term/{conversation_id}，
# 供前端「记忆」页展示长期偏好与某会话的短期记忆（摘要 + 结构化消息序列）；均按 user_id 做用户隔离。
from fastapi import APIRouter, Depends

from app.core.auth import get_current_user
from app.services.agent_service import service

router = APIRouter()


@router.get("/memory/long-term")
async def get_long_term_memory(user_id: str = Depends(get_current_user)):
    """返回当前用户的长期记忆（跨会话偏好）列表，按 importance 降序。"""
    return await service.long_term_memory.get_all(user_id)


@router.get("/memory/short-term/{conversation_id}")
async def get_short_term_memory(conversation_id: str, user_id: str = Depends(get_current_user)):
    """返回某会话的短期记忆原始记录（summary + 结构化消息序列），不做任何过滤/降级；会话归属校验失败返回空列表。"""
    # 用户隔离：会话必须归属当前用户，防止跨用户读取他人会话的短期记忆
    if not await service.conversation_store.belongs_to(user_id, conversation_id):
        return {"records": []}
    records = await service.short_term_memory.get_records(user_id, conversation_id)
    return {"records": records}
