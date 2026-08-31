# app/api/routes/conversation.py
# 会话管理路由：GET/POST /api/conversations、GET/DELETE /api/conversations/{id}，
# 均从请求头 X-User-Id 解析用户身份，透传给 ConversationStore 做用户隔离。
from fastapi import APIRouter, Header

from app.models.schemas import ConversationCreate
from app.services.agent_service import service

router = APIRouter()


@router.get("/conversations")
async def list_conversations(x_user_id: str = Header(default="anonymous")):
    """列出当前用户的所有会话（按创建时间倒序）。"""
    return await service.conversation_store.list_conversations(x_user_id)


@router.post("/conversations")
async def create_conversation(body: ConversationCreate, x_user_id: str = Header(default="anonymous")):
    """新建一个会话，返回会话 id。"""
    cid = await service.conversation_store.create_conversation(x_user_id, body.title)
    return {"conversation_id": cid}


@router.get("/conversations/{conv_id}")
async def get_conversation(conv_id: str, x_user_id: str = Header(default="anonymous")):
    """返回指定会话的历史消息列表（会话归属校验失败时返回空列表）。"""
    return await service.conversation_store.get_history(x_user_id, conv_id)


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str, x_user_id: str = Header(default="anonymous")):
    """删除指定会话及其消息、短期记忆；归属校验失败返回 deleted=False。"""
    ok = await service.delete_conversation(x_user_id, conv_id)
    return {"deleted": ok}
