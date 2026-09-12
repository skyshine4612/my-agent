# app/api/routes/knowledge.py
# 知识库路由：建库入口（粘贴文本 / 抓 URL）+ 文档列表 + 删除。
# 知识库全局共享，不做 per-user 隔离，但读写仍走 rls_conn（策略是 using(true)）。
import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.agent import _redact_secrets
from app.core.auth import get_current_user
from app.core.ingest import fetch_url_text
from app.core.storage import BUCKET_KNOWLEDGE
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


async def _ingest(user_id: str, content: str, title: str, source_url: str,
                  images: list[dict] | None = None) -> int:
    """入库并对失败打日志：切块与向量化可能跑几十秒，中途失败必须留痕。

    images 只在抓 URL 时有（粘贴文本没有图片通道），形如
    [{"n": 占位符序号, "desc", "data", "mime"}]，由 KnowledgeStore 按偏移挂块并上传。
    错误文本会随 HTTP 响应发给前端，按值抹掉已配置的凭据再外发。
    """
    try:
        return await service.knowledge.ingest(user_id, content, title=title,
                                              source_url=source_url, images=images)
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
        title, content, images = await fetch_url_text(url)
    except Exception as e:
        logger.warning("[知识库] 抓取失败 url=%s：%s", url, e)
        raise HTTPException(status_code=502, detail=f"抓取失败：{e}") from e
    # 正文太短基本是没抓到（纯 JS 渲染 / 被反爬 / 需要登录），宁可报错也不入库一堆噪声
    if len(content) < _MIN_CONTENT_LEN:
        raise HTTPException(status_code=400, detail="没抓到有效正文，可能是纯 JS 渲染、被反爬拦截或需要登录的页面")
    chunks = await _ingest(user_id, content, title=title, source_url=url, images=images)
    return {"chunks": chunks, "title": title, "source_url": url}


@router.get("/knowledge/documents")
async def list_documents(user_id: str = Depends(get_current_user)):
    """列出知识库里的文档（按来源聚合，含块数与入库时间）。"""
    return await service.knowledge.list_documents(user_id)


@router.delete("/knowledge/documents")
async def delete_document(source_url: str, user_id: str = Depends(get_current_user)):
    """按来源删除整篇文档的所有块，返回删除的块数。"""
    return {"deleted": await service.knowledge.delete_by_source(user_id, source_url)}


# 能回吐的图片类型：显式白名单，**不含 svg**。桶里可能有 svg（抓取侧现在会在下载那一步拒掉，
# 但历史对象还在），原样回吐等于把一段可执行的 XML 以内联方式送回**应用同源**——
# <img> 里不执行，但「点击放大」的直接导航会执行。
_IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
               "webp": "image/webp", "gif": "image/gif"}
# source_key 是 sha256 的前 16 位十六进制，ingest_id 是 uuid4().hex
_SOURCE_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
_INGEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
# 对象名是「占位符序号.扩展名」，序号按 MAX_IMAGES 不会超过三位
_NAME_RE = re.compile(r"^\d{1,3}\.(jpg|jpeg|png|webp|gif)$")


@router.get("/knowledge/images/{source_key}/{ingest_id}/{name}")
async def get_knowledge_image(source_key: str, ingest_id: str, name: str,
                              user_id: str = Depends(get_current_user)):
    """回吐一张知识库原图；路径形状非法、来源不存在、对象不存在一律 404（不区分，避免探测）。

    **必须带 `Depends(get_current_user)`** —— 仓库里每个路由都有，漏写它就成了未登录可读的入口。
    """
    # 路径形状校验：不校验的话这个端点就是「按 key 直读整个 knowledge 桶」的通用读口
    if not (_SOURCE_KEY_RE.match(source_key) and _INGEST_ID_RE.match(ingest_id)
            and _NAME_RE.match(name)):
        raise HTTPException(status_code=404, detail="图片不存在")
    # source_key 必须命中某个已入库文档的哈希，否则等于拿桶当匿名图床
    if not await service.knowledge.has_source_key(user_id, source_key):
        raise HTTPException(status_code=404, detail="图片不存在")
    data = await service.storage.get(user_id, BUCKET_KNOWLEDGE,
                                     f"{source_key}/{ingest_id}/{name}")
    if data is None:
        raise HTTPException(status_code=404, detail="图片不存在")
    # nosniff：扩展名白名单已经排掉 svg，这一条是防浏览器无视 Content-Type 去做嗅探
    return Response(content=data, media_type=_IMAGE_MIME[name.rsplit(".", 1)[-1]],
                    headers={"X-Content-Type-Options": "nosniff",
                             "Cache-Control": "private, max-age=300"})
