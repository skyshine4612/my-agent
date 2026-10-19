# tests/test_ingest.py
# 正文提取测试（不依赖网络）：markdown 转换是纯函数，端到端用例喂结构完整的 HTML——
# trafilatura 对残缺片段会走兜底链，行为与真实网页不同，不能拿它当输入。
from app.core.knowledge.ingest import extract_text, extract_with_images, markdown_to_text


def _marks(count: int = 1) -> str:
    """count 张图的占位符串。由实现产出，测试不硬编码它长什么样。"""
    md = "".join(f"![图](https://a.com/{i}.jpg)" for i in range(count))
    return markdown_to_text(md)[0]


def test_placeholder_is_a_single_character():
    """占位符只有一个字符——硬切的最小粒度是字符，1 个字符内部没有可下刀的缝隙。"""
    assert len(_marks(1)) == 1
    assert len(_marks(8)) == 8


def test_hard_cut_never_splits_a_placeholder():
    """扫遍所有落点：单字符占位符不可能被硬切切开，图必能回填到它归属的那一块。

    这是「图片描述无声消失」的护栏。旧实现用 8 字符占位符，硬切有 7 个缝隙可能下刀；
    切开后 `chunk.replace` 找不到完整串就跳过，正文里那张图的描述就没了，且不报错。
    1 个字符内部没有缝隙——结构性为 0，不是概率变低（2 字符实测也是 0 次，但那是样本
    没压到，不是切不开）。
    """
    from app.core.knowledge.chunking import chunk_spans, chunk_text
    from app.core.knowledge.ingest import assign_images

    mark = _marks(1)
    misses = []
    for prefix_len in range(0, 1400, 7):
        # 图文同段 + 无标点 + 超长：逼出字符硬切那一级，且让占位符正好骑在切点上
        body = "字" * prefix_len + "\n" + mark + "\n" + "尾" * 900
        chunks = chunk_text(body, 600, 100)
        got = assign_images(body, [{"n": 0, "desc": "西湖"}], chunk_spans(body, 600, 100))
        i = next((i for i, g in enumerate(got) if g), None)
        if i is None or mark not in chunks[i]:
            misses.append(prefix_len)
    assert misses == []


def test_markdown_image_becomes_placeholder():
    """图片换成占位符，让描述回填在文中真实位置而不是全堆到文末。"""
    text, images = markdown_to_text("前文\n\n![配图](https://cdn.example.com/a.jpg)\n\n后文")
    assert images == ["https://cdn.example.com/a.jpg"]
    assert text == f"前文\n\n{_marks()}\n\n后文"


def test_multiple_images_get_sequential_placeholders():
    """占位符序号即图片下标，顺序不能错位。"""
    text, images = markdown_to_text("![一](https://a.com/1.jpg)![二](https://a.com/2.jpg)")
    assert images == ["https://a.com/1.jpg", "https://a.com/2.jpg"]
    assert text == _marks(2)


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
    assert _marks(1) in text


def test_short_article_keeps_headings_and_images():
    """正文 200~800 字的页面（图集攻略的常见形态）不能被兜底纯文本顶掉格式。

    钉住 trafilatura 的 rescue 分支：主提取短于 MIN_EXTRACTED_SIZE 时它拿 baseline() 的
    扁平 <p> 整个替换结果，标题与图片一起消失，而文字量照样过建库路由的长度校验，
    于是静默入库成一篇没图、没小标题的语料。上面两个端到端用例都用 40 段（>1000 字）
    恰好绕开了这条路径，这个用例专门走进去（正文约 400 字：够入库，不够 800）。
    """
    paras = [
        "第一天从断桥残雪开始沿白堤走到平湖秋月，再到孤山看西泠印社，傍晚在楼外楼吃一顿"
        "西湖醋鱼和龙井虾仁，饭后沿湖散步看音乐喷泉，夜里住在龙翔桥附近。",
        "第二天走南线，苏堤春晓适合清晨，花港观鱼看红鱼池，中午在知味观吃小笼，"
        "下午去雷峰塔俯瞰全湖，夜里逛河坊街和南宋御街。",
        "第三天走西线，灵隐寺与飞来峰石刻值得半天，永福寺人少清静，"
        "出来后上天竺吃一碗素面，傍晚回市区。",
        "避坑经验：周末苏堤和白堤人极多，建议早上七点前进景区；"
        "龙井村的茶叶别在路边摊买；游船选国营码头，私营船漫天要价。",
    ]
    body = "".join(f"<p>{p}</p>" for p in paras)
    html = ("<html><body><article><h1>西湖三日游攻略</h1>"
            '<img src="https://cdn.example.com/1.jpg">' + body +
            '<img src="https://cdn.example.com/2.jpg">' + body +
            "</article></body></html>")
    text, images = extract_with_images(html, "https://example.com/guide/hz")
    assert images == ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.jpg"]
    assert "# 西湖三日游攻略" in text
    assert _marks(1) in text


def test_scrub_marks_removes_leftover_placeholders_and_null_bytes():
    """清掉没被回填的占位符（模型描述有 MAX_IMAGES 上限，排在后面的图没人回填）。

    要守的不变量：**输出里没有 null 字节** —— asyncpg 抛「a str must not contain
    null bytes」，丢的不是一张图，是整篇。
    """
    from app.core.knowledge.ingest import scrub_marks
    assert scrub_marks(f"前{_marks(1)}后") == "前后"      # 完整占位符整体消失
    assert scrub_marks("前\x00后") == "前后"             # 任何来源的 null 字节也挡掉


def test_assign_images_puts_image_in_the_chunk_holding_its_offset():
    """偏移法：按占位符在原文里的起始位置归属，跨边界归第一块。"""
    from app.core.knowledge.chunking import chunk_spans
    from app.core.knowledge.ingest import assign_images

    text = f"甲。{_marks(1)}\n\n乙。丙。丁。戊。己。庚。辛。壬。癸。"
    spans = chunk_spans(text, 12, 2)
    assert len(spans) >= 2
    got = assign_images(text, [{"n": 0, "desc": "西湖"}], spans)
    assert len(got) == len(spans)
    assert got[0] == [{"n": 0, "desc": "西湖"}]          # 第一张图挂在第一块
    assert all(g == [] for g in got[1:])


def test_assign_images_skips_images_without_a_mark():
    """describe_images 会 urls[:MAX_IMAGES]，IMG10+ 的图没有对应的占位符 —— 不能瞎挂。"""
    from app.core.knowledge.ingest import assign_images
    text = f"甲。{_marks(1)}乙。"
    got = assign_images(text, [{"n": 0, "desc": "a"}, {"n": 9, "desc": "b"}], [(0, len(text))])
    assert got == [[{"n": 0, "desc": "a"}]]
