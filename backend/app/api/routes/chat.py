# app/api/routes/chat.py
# SSE 流式对话路由：POST /api/agent/chat，把 AgentService.chat_stream 产出的事件流
# 包装成 Server-Sent Events 推送给前端。
import json

from fastapi import APIRouter, Header
from sse_starlette.sse import EventSourceResponse

from app.models.schemas import ChatRequest
from app.services.agent_service import service

router = APIRouter()


@router.post("/agent/chat")
async def chat(req: ChatRequest, x_user_id: str = Header(default="anonymous")):
    """SSE 流式对话端点：接收对话请求，逐事件推送 token/工具调用/结果。

    x_user_id 从请求头 X-User-Id 解析，用于用户隔离；未传时兜底 anonymous。
    """

    async def gen():
        """事件生成器：把 chat_stream 的每个事件 dict 转成 SSE 的 {event, data} 结构。"""
        # 逐条消费 service 产出的事件流，event 用 type 字段、data 用 ensure_ascii=False 保留中文
        async for ev in service.chat_stream(x_user_id, req.conversation_id, req.message):
            yield {"event": ev["type"], "data": json.dumps(ev, ensure_ascii=False)}

    # sep="\n"：明确用 LF 作行分隔符，与前端 sse.ts 的 split('\n\n') 对齐
    # （sse-starlette 默认是 \r\n，会导致前端按 \n\n 解析时永远匹配不到事件）
    return EventSourceResponse(gen(), sep="\n")
