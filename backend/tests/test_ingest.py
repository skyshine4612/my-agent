# tests/test_ingest.py
# 正文提取测试（不依赖网络）：markdown 转换是纯函数，端到端用例喂结构完整的 HTML——
# trafilatura 对残缺片段会走兜底链，行为与真实网页不同，不能拿它当输入。
from app.core.ingest import extract_text, extract_with_images, markdown_to_text


def test_markdown_image_becomes_placeholder():
    """图片换成占位符，让描述回填在文中真实位置而不是全堆到文末。"""
    text, images = markdown_to_text("前文\n\n![配图](https://cdn.example.com/a.jpg)\n\n后文")
    assert images == ["https://cdn.example.com/a.jpg"]
    assert text == "前文\n\n\x00IMG0\x00\n\n后文"


def test_multiple_images_get_sequential_placeholders():
    """占位符序号即图片下标，顺序不能错位。"""
    text, images = markdown_to_text("![一](https://a.com/1.jpg)![二](https://a.com/2.jpg)")
    assert images == ["https://a.com/1.jpg", "https://a.com/2.jpg"]
    assert text == "\x00IMG0\x00\x00IMG1\x00"


def test_drops_svg_and_unreachable_images():
    """svg 是图标/logo、data URI 取不到，两者连占位符一起丢弃，不留空壳。"""
    text, images = markdown_to_text(
        "前文 ![图标](https://example.com/logo.svg) 中 ![内联](data:image/gif;base64,R0lGOD) 后文")
    assert images == []
    assert text == "前文  中  后文"


def test_resolves_protocol_relative_image_url():
    """CDN 普遍写协议相对地址，漏掉会一张图都采不到。"""
    _, images = markdown_to_text("![图](//cdn.example.com/a.jpg)")
    assert images == ["https://cdn.example.com/a.jpg"]


def test_resolves_root_relative_image_url():
    """根相对地址用页面地址补全。"""
    _, images = markdown_to_text("![图](/img/a.jpg)", "https://example.com/guide/hz")
    assert images == ["https://example.com/img/a.jpg"]


def test_link_keeps_text_drops_url():
    """链接文本是正文的一部分，URL 进知识库只会是噪声。"""
    text, _ = markdown_to_text("大熊猫是[食肉动物](https://baike.baidu.com/item/6761)，属于熊科。")
    assert text == "大熊猫是食肉动物，属于熊科。"


def test_strips_inline_emphasis_keeps_headings():
    """粗体与行内代码标记去掉，标题的 # 保留——层级对语义有用。"""
    text, _ = markdown_to_text("# 大熊猫\n\n**重点**内容与`代码`")
    assert text == "# 大熊猫\n\n重点内容与代码"


def test_extracts_article_body():
    """端到端：结构完整的文章页能提到正文。"""
    body = "".join(
        f"<p>第{i}段：西湖是杭州最著名的景点，四季景色各异，值得慢慢游览一遍。</p>"
        for i in range(1, 41))
    html = f"<html><body><article><h1>杭州攻略</h1>{body}</article></body></html>"
    text = extract_text(html)
    assert "西湖是杭州最著名的景点" in text
    assert "第40段" in text


def test_empty_html():
    """空输入返回空串，不抛异常。"""
    assert extract_text("") == ""


def test_extract_with_images_returns_both():
    """端到端：正文里的图片进 URL 列表，占位符留在正文里。"""
    body = "".join(
        f"<p>第{i}段：西湖是杭州最著名的景点，四季景色各异，值得慢慢游览一遍。</p>"
        for i in range(1, 41))
    html = f'<html><body><article>{body}<img src="https://cdn.example.com/a.jpg"></article></body></html>'
    text, images = extract_with_images(html, "https://example.com/guide/hz")
    assert images == ["https://cdn.example.com/a.jpg"]
    assert "\x00IMG0\x00" in text
