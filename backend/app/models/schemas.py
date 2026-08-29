# app/models/schemas.py
# Pydantic 请求体模型：定义 API 层的入参结构（对话请求、新建会话请求）。
from pydantic import BaseModel


class ChatRequest(BaseModel):
    """对话请求体：conversation_id 可选（首次为 None），message 必填"""
    conversation_id: str | None = None
    message: str


class ConversationCreate(BaseModel):
    """新建会话请求体"""
    title: str = "新会话"
