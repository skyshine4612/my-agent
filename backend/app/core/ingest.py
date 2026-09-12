# app/core/ingest.py
# 建库管道：把 URL 抓成正文文本。对话时一律不爬，只有前端建库入口会走到这里。
import asyncio
import logging
import re
import time
from copy import deepcopy
from urllib.parse import urljoin

import trafilatura
from trafilatura.settings import DEFAULT_CONFIG

from app.core.vision import describe_images

logger = logging.getLogger(__name__)

# 图片占位符：正文里留住图的位置，描述生成后原地回填
_IMAGE_MARK = r"\x00IMG\d+\x00"

# 主提取产出低于此值才会去走 readability / jusText 兜底链。默认的 250 太低：主提取只拿到
# 几百字导航的页面会直接返回导航、触发不了兜底，正文整块丢。代价是本来干净的结果可能混进
# 「登录/无障碍」这类短碎片，对检索无害。
_EXTRACTOR_CONFIG = deepcopy(DEFAULT_CONFIG)
_EXTRACTOR_CONFIG["DEFAULT"]["MIN_EXTRACTED_SIZE"] = "800"

# trafilatura 的 markdown 图片与链接语法
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)[^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 行内标记：粗体与行内代码只留内容。标题的 # 保留，层级对语义有用。
_MD_INLINE = re.compile(r"\*\*|`")


def _image_url(src: str, base_url: str) -> str:
    """补全图片地址：协议相对（//host/a.jpg）补 https，根相对（/a.jpg）用页面地址补。"""
    if src.startswith("//"):
        return "https:" + src
    return urljoin(base_url, src)


def _keep_image(url: str) -> bool:
    """筛掉交付不了或注定是噪声的图片地址。传补全后的地址——判据只看最终形态。

    svg 基本是图标/logo，交给多模态模型只会得到噪声；非 http(s) 的取不到。
    其余图标、二维码、装饰图靠模型自判（vision 侧返回「无有效信息」即丢弃）。
    """
    if not url.startswith(("http://", "https://")):
        return False
    return not url.split("?")[0].lower().endswith(".svg")


def markdown_to_text(md: str, base_url: str = "") -> tuple[str, list[str]]:
    """trafilatura 的 markdown 输出 → (带图片占位的正文, 按出现顺序的图片 URL 列表)。

    图片换成占位符，让描述回填在文中真实位置而不是全堆到文末；链接只留文本，
    URL 进知识库对检索没有价值。被筛掉的图片连占位符一起丢弃，不留空壳。
    """
    images: list[str] = []
    parts: list[str] = []
    last = 0
    for m in _MD_IMAGE.finditer(md):
        parts.append(md[last:m.start()])
        last = m.end()
        url = _image_url(m.group(1), base_url)
        if not _keep_image(url):
            continue
        parts.append(f"\x00IMG{len(images)}\x00")
        images.append(url)
    parts.append(md[last:])
    text = _MD_LINK.sub(r"\1", "".join(parts))
    return _MD_INLINE.sub("", text), images


def _normalize(text: str) -> str:
    """连续空行压成一个空行、去首尾空白——段落边界与 chunk_text 的切分粒度对齐。"""
    paras = (p.strip() for p in re.split(r"\n\s*\n", text))
    return "\n\n".join(p for p in paras if p)


def extract_with_images(html: str, base_url: str = "") -> tuple[str, list[str]]:
    """HTML → (带图片占位的正文, 按出现顺序的图片 URL 列表)。

    正文由 trafilatura 提取（配置见 _EXTRACTOR_CONFIG），本函数只把它输出的 markdown
    转成建库管道要的形状。base_url 传页面地址：trafilatura 靠它启用站点特定规则。
    """
    md = trafilatura.extract(
        html,
        url=base_url or None,
        output_format="markdown",
        include_tables=True,
        include_images=True,
        include_links=True,
        # 攻略页的「用户问答」落在评论区容器里，对检索有价值，不按噪音丢弃
        include_comments=True,
        config=_EXTRACTOR_CONFIG,
    ) or ""
    text, images = markdown_to_text(md, base_url)
    return _normalize(text), images


def extract_text(html: str, base_url: str = "") -> str:
    """HTML → 正文纯文本（图片位置留空）。

    段落边界与 chunk_text 的切分粒度对齐，抓下来的正文可以直接进切块管道。
    """
    text, _ = extract_with_images(html, base_url)
    return re.sub(_IMAGE_MARK, "", text)


def _mark_offsets(text: str) -> dict[int, int]:
    """占位符序号 → 它在正文里的起始偏移。"""
    return {int(m.group()[4:-1]): m.start() for m in re.finditer(_IMAGE_MARK, text)}


def _chunk_of(spans: list[tuple[int, int]], offset: int) -> int | None:
    """偏移落在哪一块（跨边界归第一块）；落在所有块之前/之后时夹到最近的一块。"""
    if not spans:
        return None
    for i, (s, e) in enumerate(spans):
        if s <= offset < e:
            return i
    return 0 if offset < spans[0][0] else len(spans) - 1


def assign_images(text: str, images: list[dict], spans: list[tuple[int, int]]) -> list[list[dict]]:
    """把图片按「起始偏移落在哪一块」挂到唯一一块，返回与 spans 等长的列表。

    为什么不用「占位符穿过 chunk_text、切完逐块扫」：`chunk_text` 的字符硬切分支
    （piece = sent[i:i + chunk_size]）会把 8 字符的 \\x00IMG3\\x00 从中间切开，前半截留在
    上一块、匹配不上完整占位符，\\x00 就原样进了 documents.content —— asyncpg 直接抛
    「a str must not contain null bytes」，丢的不是一张图而是整篇，且表现为「偶发抓取失败」。

    找不到对应占位符的图（describe_images 内部 urls[:MAX_IMAGES] 会让 IMG10+ 无人回填）
    直接跳过，不瞎挂。
    """
    buckets: list[list[dict]] = [[] for _ in spans]
    offsets = _mark_offsets(text)
    for img in images:
        offset = offsets.get(img["n"])
        if offset is None:
            continue
        i = _chunk_of(spans, offset)
        if i is not None:
            buckets[i].append(img)
    return buckets


def scrub_marks(chunk: str) -> str:
    """入库前兜底清掉占位符与 \\x00 残留。**不可省略**：

    除了 IMG10+，硬切分支切开的半截占位符也靠它兜住 —— 留下一个 \\x00 就是整篇 502。
    """
    return re.sub(_IMAGE_MARK, "", chunk).replace("\x00", "")


def _fetch_html_sync(url: str) -> tuple[str, str]:
    """用 playwright 打开 URL（跑完 JS 再取 DOM），返回 (标题, HTML)。

    走同步 API：它在本线程内新建一个独立的事件循环（取全局 asyncio 策略，
    Windows 上默认是 Proactor），driver 由该循环经 create_subprocess_exec 拉起。
    宿主循环是 Selector（Windows 下 uvicorn reload 即如此）也不影响它 —— 前提是
    全局策略没被改成 Selector，uvicorn 只换宿主循环的工厂，不动全局策略。
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 再等异步渲染落地；等不到就用已有 DOM 继续，不因为等超时而整个失败
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                logger.info("[知识库] networkidle 等待超时，按当前 DOM 提取 url=%s", url)
            title, html = (page.title() or url), page.content()
            logger.info("[知识库] 页面就绪 url=%s 标题=%s HTML=%d 字", url, title, len(html))
            return title, html
        finally:
            browser.close()


async def _describe_images_indexed(urls: list[str], referer: str = "") -> list[dict]:
    """描述图片并附上它在正文里的占位符序号。

    原始序号用 enumerate 保住：describe_images 内部会 urls[:MAX_IMAGES]，返回列表比 urls 短，
    但前 N 张的序号仍与占位符 \\x00IMG{i}\\x00 一一对应。
    """
    if not urls:
        return []
    results = await describe_images(urls, referer=referer)
    return [{"n": i, "desc": r["desc"], "data": r["data"], "mime": r["mime"]}
            for i, r in enumerate(results) if r["desc"]]


async def fetch_url_text(url: str) -> tuple[str, str, list[dict]]:
    """用 playwright 打开 URL（跑完 JS 再取 DOM），返回 (标题, 带图片占位的正文, 图片列表)。

    **不做回填**：回填要等切块之后才知道每张图归哪一块（偏移法），而图片字节要一路留到
    KnowledgeStore.ingest —— 那里才有 user_id 可用来上传。
    图片列表形如 [{"n": 占位符序号, "desc", "data", "mime"}]，无描述的项已剔除。

    攻略站多为前端渲染，直接抓 HTML 拿不到正文，所以走真实浏览器。
    建库是低频操作，每次起一个浏览器实例即可，不做常驻连接池。
    """
    logger.info("[知识库] 开始抓取 url=%s", url)
    started = time.monotonic()
    title, html = await asyncio.to_thread(_fetch_html_sync, url)
    text, urls = extract_with_images(html, url)
    images = await _describe_images_indexed(urls, referer=url)
    logger.info("[知识库] 抓取完成 url=%s 正文 %d 字，图片候选 %d 张，拿到描述 %d 张，耗时 %.1fs",
                url, len(text), len(urls), len(images), time.monotonic() - started)
    return title, text, images
