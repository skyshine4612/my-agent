# app/api/routes/knowledge.py
# 知识库路由：建库入口（粘贴文本 / 抓 URL）+ 文档列表 + 删除。
# 知识库全局共享，不做 per-user 隔离，但读写仍走 rls_conn（策略是 using(true)）。
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.agent import _redact_secrets
from app.core.auth import get_current_user
from app.core.ingest import fetch_url_text
from app.services.agent_service import service

logger = logging.getLogger(__name__)

router = APIRouter()

# 抓到的正文短于此值就不入库。真实短页面都在 270 字以上（澎湃详情 278、政府网政策 323、
# 新华网领导人 456），而抓不到时同样会产出「内容」——知乎限流页返回的是一段 117 字的
# JSON 错误体，越过阈值就成了知识库里的噪声。
_MIN_CONTENT_LEN = 200


class TextIn(BaseModel):
    """粘贴文本入库的请求体。"""

    content: str = Field(min_length=1)
    title: str = ""
    source_url: str = ""


class UrlIn(BaseModel):
    """URL 抓取入库的请求体。"""

    url: str = Field(min_length=1)


def _fallback_title(content: str, title: str) -> str:
    """没填标题就用正文开头当标题，保证列表里有个能认出来的名字。"""
    return title.strip() or content.strip()[:20]


async def _ingest(user_id: str, content: str, title: str, source_url: str) -> int:
    """入库并对失败打日志：切块与向量化可能跑几十秒，中途失败必须留痕。

    错误文本会随 HTTP 响应发给前端，按值抹掉已配置的凭据再外发。
    """
    try:
        return await service.knowledge.ingest(user_id, content, title=title, source_url=source_url)
    except Exception as e:
        logger.exception("[知识库] 入库失败 source=%s", source_url)
        raise HTTPException(status_code=502, detail=f"入库失败：{_redact_secrets(str(e))}") from e


@router.post("/knowledge/text")
async def ingest_text(body: TextIn, user_id: str = Depends(get_current_user)):
    """粘贴文本入库：切块 + 向量化 + 写入 documents，返回写入的块数。"""
    # 文本入口没有真实 URL，但 source_url 是文档的唯一标识（列表聚合 / 删除都靠它），
    # 所以生成一个合成标识补上，保证任何文档都能被删掉。
    source = body.source_url.strip() or f"text://{uuid.uuid4().hex}"
    chunks = await _ingest(user_id, body.content,
                           title=_fallback_title(body.content, body.title), source_url=source)
    if not chunks:
        raise HTTPException(status_code=400, detail="内容为空，没有可入库的文本")
    return {"chunks": chunks, "source_url": source}


@router.post("/knowledge/url")
async def ingest_url(body: UrlIn, user_id: str = Depends(get_current_user)):
    """抓取 URL 正文入库（只在建库阶段爬，对话时一律不爬）。"""
    url = body.url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL 必须以 http:// 或 https:// 开头")
    try:
        title, content = await fetch_url_text(url)
    except Exception as e:
        logger.warning("[知识库] 抓取失败 url=%s：%s", url, e)
        raise HTTPException(status_code=502, detail=f"抓取失败：{e}") from e
    # 正文太短基本是没抓到（纯 JS 渲染 / 被反爬 / 需要登录），宁可报错也不入库一堆噪声
    if len(content) < _MIN_CONTENT_LEN:
        raise HTTPException(status_code=400, detail="没抓到有效正文，可能是纯 JS 渲染、被反爬拦截或需要登录的页面")
    chunks = await _ingest(user_id, content, title=title, source_url=url)
    return {"chunks": chunks, "title": title, "source_url": url}


@router.get("/knowledge/documents")
async def list_documents(user_id: str = Depends(get_current_user)):
    """列出知识库里的文档（按来源聚合，含块数与入库时间）。"""
    return await service.knowledge.list_documents(user_id)


@router.delete("/knowledge/documents")
async def delete_document(source_url: str, user_id: str = Depends(get_current_user)):
    """按来源删除整篇文档的所有块，返回删除的块数。"""
    return {"deleted": await service.knowledge.delete_by_source(user_id, source_url)}
