# app/core/vision.py
# 图片转文字：把攻略页里的插图交给多模态模型生成描述。
# 知识库是文本语义检索，图片不转成文字就搜不到——这是让图片内容可被检索的唯一途径。
import asyncio
import base64
import logging
from abc import ABC, abstractmethod

import httpx
from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

# 单页最多转多少张：攻略页动辄几十张图，全转又慢又贵；正文主图通常靠前，取前若干张
MAX_IMAGES = 10
# 下载图片用的请求头：图床/CDN 普遍按 UA 拦截，httpx 的默认 UA 会被直接回 403
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}
# 小于这个体积的多半是图标/头像/占位图，转出来只会是噪声
MIN_IMAGE_BYTES = 10 * 1024
# 能进桶的图片类型 → 扩展名。**白名单**：扩展名要拿去定代理端点的 Content-Type，
# 是 XSS 面的一部分（svg 是文本可执行的 XML，`<img>` 里不执行、直接导航会执行）；
# 而 image/bmp 这类少见格式不值得为它多开一条回吐路径。
ALLOWED_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

_PROMPT = ("这是一张旅行攻略文章里的插图。用一两句中文描述画面内容，重点写对行程规划有用的信息："
           "景点或地标名称、建筑与自然景观特征、画面里的价格/时间/菜单等文字。"
           "如果画面没有有效信息（纯装饰图、广告位、头像），只回复「无有效信息」。")


class VisionClient(ABC):
    """多模态接口：把一张图描述成文字。"""

    @abstractmethod
    async def describe(self, image: bytes, mime: str) -> str:
        """返回图片的文字描述。"""
        ...


class OpenAICompatVision(VisionClient):
    """OpenAI 兼容协议的多模态实现（DashScope qwen-vl 系列）。"""

    def __init__(self, base_url, api_key, model):
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key,
                                  timeout=httpx.Timeout(60.0, connect=10.0))

    async def describe(self, image: bytes, mime: str = "image/jpeg") -> str:
        # 传 base64 data URI 而非图片 URL：站点图片常带防盗链，让模型侧去拉多半拉不到
        b64 = base64.b64encode(image).decode()
        resp = await self.client.chat.completions.create(
            model=self.model, max_tokens=200,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": _PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]}])
        return (resp.choices[0].message.content or "").strip()


# 进程级单例：与 embedder 同构，首次调用时才构造
_vision: VisionClient | None = None


def get_vision() -> VisionClient:
    """返回全局多模态客户端单例（首次调用时构造）。

    VISION_MODEL / VISION_API_KEY 必须显式配在 .env 里：这里不做「留空则复用 llm_*」的兜底，
    兜底会让「没配」和「配错了」长得一模一样，只能靠读代码才分得清。
    """
    global _vision
    if _vision is None:
        _vision = OpenAICompatVision(
            base_url=settings.llm_base_url,
            api_key=settings.vision_api_key,
            model=settings.vision_model)
    return _vision


async def _download(http: httpx.AsyncClient, url: str):
    """下载图片，返回 (字节, mime)；下载失败、mime 不在白名单、体积过小一律返回 None。"""
    try:
        resp = await http.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        logger.info("[知识库] 图片下载失败，跳过 url=%s：%s", url, e)
        return None
    mime = resp.headers.get("content-type", "").split(";")[0].strip()
    if mime not in ALLOWED_MIME or len(resp.content) < MIN_IMAGE_BYTES:
        return None
    return resp.content, mime


async def _describe_one(vision, http, url: str) -> dict:
    """下载并描述一张图；任何一步失败都返回空条目，不影响其他图与整篇入库。"""
    got = await _download(http, url)
    if got is None:
        return {"desc": "", "data": None, "mime": ""}
    data, mime = got
    try:
        desc = await vision.describe(data, mime)
    except Exception as e:
        logger.info("[知识库] 图片描述失败，跳过 url=%s：%s", url, e)
        return {"desc": "", "data": None, "mime": ""}
    # 模型自己判断没信息的图直接丢掉，别把「无有效信息」这五个字塞进正文。
    # 字节一并丢：既不进正文就不该进桶。
    if "无有效信息" in desc:
        return {"desc": "", "data": None, "mime": ""}
    return {"desc": desc, "data": data, "mime": mime}


async def describe_images(urls: list[str], vision=None, referer: str = "") -> list[dict]:
    """并发描述多张图，返回与传入 urls 等长的列表，每项 {"desc", "data", "mime"}。

    **返回字节而不只是描述**：原图要一起进 Storage，重下一次就是多一轮网络往返。
    vision 可注入（测试用 Fake），缺省用全局单例。
    referer 传页面地址：图床普遍做防盗链，不带 Referer 的请求会被回 403，抓不到图。
    """
    urls = urls[:MAX_IMAGES]
    if not urls:
        return []
    vision = vision or get_vision()
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    async with httpx.AsyncClient(headers=headers) as http:
        return list(await asyncio.gather(*(_describe_one(vision, http, u) for u in urls)))
