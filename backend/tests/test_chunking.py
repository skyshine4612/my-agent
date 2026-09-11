# tests/test_chunking.py
# 切块纯函数测试：覆盖三级降级（段落 / 句子 / 字符硬切）与边界情形。
from app.core.chunking import chunk_text


def test_short_text_stays_single_chunk():
    """短文本不切，原样一块。"""
    assert chunk_text("西湖值得去。", chunk_size=600) == ["西湖值得去。"]


def test_split_by_blank_line():
    """段落边界优先：多段短文本各成一块，空段被丢掉。"""
    text = "第一段\n\n\n\n第二段\n\n第三段"
    assert chunk_text(text, chunk_size=600) == ["第一段", "第二段", "第三段"]


def test_long_paragraph_splits_on_sentence():
    """超长段落按句末标点切开，且句子内容不被破坏。"""
    sents = ["第一句讲西湖的玩法。", "第二句讲灵隐寺的时间！", "第三句讲避坑要点？"]
    text = "".join(sents)
    out = chunk_text(text, chunk_size=12)
    assert out == sents


def test_hard_split_keeps_overlap():
    """无标点长串硬切时，相邻块之间保留 overlap 个字符的重叠。"""
    text = "a" * 100
    out = chunk_text(text, chunk_size=30, overlap=10)
    assert all(len(c) <= 30 for c in out)
    # 第二块的开头 10 字符应与第一块的结尾 10 字符一致（重叠区）
    assert out[1][:10] == out[0][-10:]
    # 覆盖到尾部，不丢内容
    assert out[-1].endswith("a" * 10)


def test_blank_and_whitespace_paragraphs_skipped():
    """纯空白段落不产出空块。"""
    assert chunk_text("\n\n   \n\n\n\n", chunk_size=100) == []
