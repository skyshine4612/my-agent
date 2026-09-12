# app/models/schemas.py
# Pydantic 请求体模型：定义 API 层的入参结构（对话请求、新建会话请求）。
from pydantic import BaseModel


class ChatFile(BaseModel):
    """用户消息携带的附件：只留气泡展示所需的三项（file_id 供下载，name/size 供展示）。"""
    file_id: str
    name: str
    size: int = 0


class ChatRequest(BaseModel):
    """对话请求体：conversation_id 可选（首次为 None），message 必填，files 为随消息发出的附件"""
    conversation_id: str | None = None
    message: str
    files: list[ChatFile] = []


class ConversationCreate(BaseModel):
    """新建会话请求体"""
    title: str = "新会话"
