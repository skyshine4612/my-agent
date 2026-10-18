# app/core/ingest.py
# 建库管道：把 URL 抓成正文文本。对话时一律不爬，只有前端建库入口会走到这里。
import asyncio
import logging
import re
import time
from urllib.parse import urljoin

import trafilatura

from app.core.vision import describe_images

logger = logging.getLogger(__name__)

# 图片占位符：正文里留住图的位置，描述生成后原地回填。
# **必须只占一个字符**——字符硬切的最小粒度就是字符，1 个字符内部没有可下刀的缝隙，
# 切点只能落在它左边或右边。曾用 `\x00IMG{n}\x00`（6 字符、内部 5 个缝隙）：实测扫 200 个
# 落点有 1 个正好骑在切点上，那一块只含前半截，`replace` 找不到完整串就跳过，图片描述
# 无声消失且不报错。单字符把这件事从「低概率」变成「结构性不可能」。
_PUA_START = 0xE000
_MARK_RE = re.compile(f"[{chr(_PUA_START)}-{chr(_PUA_START + 0x18FF)}]")


def image_mark(n: int) -> str:
    """第 n 张图的占位符：一个私用区字符（U+E000 起，共 6400 个）。

    私用区在正常网页正文里不会出现，拿它当标记不会与正文撞车。
    """
    return chr(_PUA_START + n)


# 提取用 trafilatura 的默认配置（只关 include_comments，见该参数处的注释），
# **刻意不覆盖 MIN_EXTRACTED_SIZE**：主提取结果短于该值时，
# 它会拿 baseline() 的扁平 <p> 纯文本整个替换掉结果 —— 正文捞得更多，标题与图片却一起没了。
# 曾把它调到 800 想让「主提取只抓到几百字导航就当成功」的页面进兜底链，实测三组页面
# （导航淹没正文、导航页、正文 400 字的图集页）里 800 档一次都没多捞到正文，只把带图的
# markdown 换成了无格式纯文本；「抓得太少」trafilatura 2.2.0 自己会用 escalation 判断
# （提取量不足页面文本 20% 时换 recall 模式重试），不需要这个旋钮。

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
        parts.append(image_mark(len(images)))
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

    正文由 trafilatura 提取：显式要 markdown 输出并保留表格 / 图片 / 链接（建库管道需要），
    同时关掉评论区（理由见该参数处的注释）；其余走默认配置。为什么不调
    MIN_EXTRACTED_SIZE 见文件头。本函数只把输出的 markdown 转成建库管道要的形状。
    base_url 传页面地址：trafilatura 靠它启用站点特定规则。
    """
    md = trafilatura.extract(
        html,
        url=base_url or None,
        output_format="markdown",
        include_tables=True,
        include_images=True,
        include_links=True,
        # 丢评论区：trafilatura 默认收，但攻略站的评论区主要是「5分 超棒 举报点赞」这类点评，
        # 没有检索价值，还会稀释正文块。
        include_comments=False,
    ) or ""
    text, images = markdown_to_text(md, base_url)
    return _normalize(text), images


def extract_text(html: str, base_url: str = "") -> str:
    """HTML → 正文纯文本（图片位置留空）。

    段落边界与 chunk_text 的切分粒度对齐，抓下来的正文可以直接进切块管道。
    """
    text, _ = extract_with_images(html, base_url)
    return _MARK_RE.sub("", text)


def _mark_offsets(text: str) -> dict[int, int]:
    """占位符序号 → 它在正文里的起始偏移。"""
    return {ord(m.group()) - _PUA_START: m.start() for m in _MARK_RE.finditer(text)}


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

    为什么不用「占位符穿过 chunk_text、切完逐块扫」：硬切级带 overlap，重叠区里的占位符
    会同时出现在相邻两块里，逐块扫就把它挂两遍、图片描述在正文里重复一遍。偏移法天然
    只挂一块（跨边界归第一块）。

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
    """入库前兜底清掉没被回填的占位符。**不可省略**：

    模型描述有 MAX_IMAGES 上限，排在后面的图没人回填，占位符会原样留在正文里。
    \\x00 也一并挡掉：正常网页文本不该有它，但 asyncpg 会抛「a str must not contain
    null bytes」——丢的不是一张图，是整篇。
    """
    return _MARK_RE.sub("", chunk).replace("\x00", "")


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


async def _describe_images_indexed(urls: list[str], referer: str = "",
                                   context: str = "") -> list[dict]:
    """描述图片并附上它在正文里的占位符序号。

    原始序号用 enumerate 保住：describe_images 内部会 urls[:MAX_IMAGES]，返回列表比 urls 短，
    但前 N 张的序号仍与占位符 image_mark(i) 一一对应。context 是页面标题，转给模型做判定。
    """
    if not urls:
        return []
    results = await describe_images(urls, referer=referer, context=context)
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
    images = await _describe_images_indexed(urls, referer=url, context=title)
    logger.info("[知识库] 抓取完成 url=%s 正文 %d 字，图片候选 %d 张，拿到描述 %d 张，耗时 %.1fs",
                url, len(text), len(urls), len(images), time.monotonic() - started)
    return title, text, images
