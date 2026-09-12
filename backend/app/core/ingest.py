# app/core/ingest.py
# 建库管道：把 URL 抓成正文文本。对话时一律不爬，只有前端建库入口会走到这里。
import asyncio
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

from app.core.vision import describe_images

logger = logging.getLogger(__name__)

# 非正文标签：整棵子树丢弃（head 里的 title/meta 也算，标题另外用 page.title() 取）
_SKIP_TAGS = {"head", "script", "style", "noscript", "svg", "iframe", "canvas",
              "nav", "header", "footer", "aside", "form", "button", "select"}
# 块级标签：进出各补一个换行，避免段落被黏成一行
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4",
               "h5", "h6", "section", "article", "blockquote", "pre", "figure"}

# 图片占位符：正文里留住图的位置，描述生成后原地回填
_IMAGE_MARK = r"\x00IMG\d+\x00"


class _TextExtractor(HTMLParser):
    """HTML → 纯文本：跳过非正文标签，块级标签处断行，图片位置留占位符。"""

    def __init__(self, base_url: str = ""):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # 按出现顺序记图片 src，占位符里的序号即这里的下标
        self.images: list[str] = []
        # 页面地址：用于把根相对路径（/a.jpg）补成绝对 URL
        self.base_url = base_url
        # 嵌套的 script/style 要按深度计数，否则内层闭合后就提前恢复采集
        self._skip_depth = 0

    def _image_src(self, attrs: dict) -> str:
        """从 img 标签属性里挑出可下载的图片地址，挑不出返回空串。

        懒加载站点把真身放在 data-src 一类属性、src 只留占位图，所以 src 为空或 data URI
        时回退到这些属性。地址还得是绝对或可补全的：协议相对（//host/a.jpg）直接补 https，
        根相对（/a.jpg）用页面地址补，data URI 是站点内联的小图标、一律丢弃。
        """
        src = attrs.get("src", "")
        if not src or src.startswith("data:"):
            src = (attrs.get("data-src") or attrs.get("data-original")
                   or attrs.get("data-lazy-src") or "")
        if not src or src.startswith("data:"):
            return ""
        full = "https:" + src if src.startswith("//") else urljoin(self.base_url, src)
        # svg 基本是图标/logo，交给多模态模型只会返回噪声
        if not full.startswith(("http://", "https://")) or full.split("?")[0].lower().endswith(".svg"):
            return ""
        return full

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "img":
            if self._skip_depth == 0:
                src = self._image_src(dict(attrs))
                if src:
                    self.parts.append(f"\x00IMG{len(self.images)}\x00")
                    self.images.append(src)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            # 源码里的换行/缩进只是排版，一律压成空格；段落边界只能由块级标签产生，
            # 否则源码怎么折行会直接影响切块结果。
            self.parts.append(re.sub(r"\s+", " ", data))


def extract_with_images(html: str, base_url: str = "") -> tuple[str, list[str]]:
    """HTML → (带图片占位的正文, 按出现顺序的图片 URL 列表)。

    占位符让图片描述回填在它在文中的真实位置附近，而不是全堆到文末。
    base_url 是页面地址，用于补全根相对路径的图片。
    """
    p = _TextExtractor(base_url)
    p.feed(html)
    lines = [ln.strip() for ln in "".join(p.parts).split("\n")]
    return "\n\n".join(ln for ln in lines if ln), p.images


def extract_text(html: str, base_url: str = "") -> str:
    """HTML → 正文纯文本（图片位置留空）。

    段落边界与 chunk_text 的切分粒度对齐，抓下来的正文可以直接进切块管道。
    """
    text, _ = extract_with_images(html, base_url)
    return re.sub(_IMAGE_MARK, "", text)


async def _fill_image_descriptions(text: str, urls: list[str], referer: str = "") -> str:
    """把正文里的图片占位符换成多模态模型给的描述。

    拿不到描述的（下载失败 / 模型报错 / 模型判定无有效信息）直接删掉占位符——
    留一个「[图片]」空壳对检索只是噪声。超出单页上限的占位符同样被清掉。
    referer 传页面地址，供图床的防盗链校验。
    """
    if not urls:
        return text
    descs = await describe_images(urls, referer=referer)
    for i, desc in enumerate(descs):
        text = text.replace(f"\x00IMG{i}\x00", f"【图片：{desc}】" if desc else "")
    return re.sub(_IMAGE_MARK, "", text)


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
            return (page.title() or url), page.content()
        finally:
            browser.close()


async def fetch_url_text(url: str) -> tuple[str, str]:
    """用 playwright 打开 URL（跑完 JS 再取 DOM），返回 (标题, 正文文本)。

    攻略站多为前端渲染，直接抓 HTML 拿不到正文，所以走真实浏览器。
    建库是低频操作，每次起一个浏览器实例即可，不做常驻连接池。
    """
    title, html = await asyncio.to_thread(_fetch_html_sync, url)
    text, images = extract_with_images(html, url)
    return title, await _fill_image_descriptions(text, images, referer=url)
