# tests/test_ingest.py
# 正文提取纯函数测试（不依赖网络）：URL 抓取本身按计划 §9 走手动验证，不写自动化用例。
from app.core.ingest import extract_text, extract_with_images


def test_drops_script_and_style():
    """script/style 的内容不进正文，哪怕里面写着看似有用的字。"""
    html = "<body><script>var x='西湖';</script><style>.a{color:red}</style><p>正文</p></body>"
    assert extract_text(html) == "正文"


def test_keeps_paragraph_breaks():
    """块级标签断行，两个 p 之间是空行（与 chunk_text 的段落边界对齐）。"""
    assert extract_text("<div><p>第一段</p><p>第二段</p></div>") == "第一段\n\n第二段"


def test_drops_nav_header_footer():
    """导航/页眉/页脚整块丢弃，只留正文。"""
    html = "<header>站点标题</header><nav>首页 攻略</nav><p>正文一</p><footer>版权</footer>"
    assert extract_text(html) == "正文一"


def test_source_newlines_do_not_create_paragraphs():
    """HTML 源码里的折行只是排版，压成空格——分段只认块级标签。"""
    assert extract_text("<p>西湖   很好\n玩</p>") == "西湖 很好 玩"


def test_drops_head_title():
    """head 里的 title 不作为正文（标题另由浏览器 API 取）。"""
    assert extract_text("<head><title>杭州攻略</title></head><body>正文</body></html>") == "正文"


def test_empty_html():
    """空输入返回空串，不抛异常。"""
    assert extract_text("") == ""


def test_resolves_protocol_relative_image_url():
    """协议相对地址（//host/a.jpg）补成 https——CDN 站点普遍这么写，漏掉会一张图都采不到。"""
    _, images = extract_with_images('<p>正文</p><img src="//cdn.example.com/a.jpg">')
    assert images == ["https://cdn.example.com/a.jpg"]


def test_resolves_root_relative_image_url():
    """根相对地址（/a.jpg）用页面地址补全。"""
    _, images = extract_with_images('<p>正文</p><img src="/img/a.jpg">', "https://example.com/guide/hz")
    assert images == ["https://example.com/img/a.jpg"]


def test_falls_back_to_lazy_loading_attr():
    """懒加载站点把真身放 data-src、src 只留占位图，占位图不是有效地址。"""
    html = '<p>正文</p><img src="data:image/gif;base64,R0lGOD" data-src="https://cdn.example.com/a.jpg">'
    _, images = extract_with_images(html)
    assert images == ["https://cdn.example.com/a.jpg"]


def test_drops_svg_icons():
    """svg 基本是图标/logo，交给多模态模型只会得到噪声。"""
    _, images = extract_with_images(
        '<p>正文</p><img src="https://example.com/logo.svg"><img src="/icon.svg?ver=2">')
    assert images == []


def test_drops_images_outside_body():
    """导航/页脚里的图不入正文（与正文文本的丢弃规则一致）。"""
    html = '<nav><img src="https://cdn.example.com/logo.png"></nav><p>正文</p>'
    assert extract_text(html) == "正文"
