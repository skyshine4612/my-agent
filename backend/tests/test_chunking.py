# tests/test_chunking.py
# 切块纯函数测试：覆盖累积成块与三级降级（段落 / 句子 / 字符硬切）的边界情形。
from app.core.chunking import chunk_spans, chunk_text, chunk_units


def test_short_text_stays_single_chunk():
    """短文本不切，原样一块。"""
    assert chunk_text("西湖值得去。", chunk_size=600) == ["西湖值得去。"]


def test_short_paragraphs_merge_into_one_chunk():
    """短段落累积成一块，块内保留段落边界（换行分隔），空段被丢掉。"""
    text = "第一段\n\n\n\n第二段\n\n第三段"
    assert chunk_text(text, chunk_size=600) == ["第一段\n第二段\n第三段"]


def test_chunk_never_exceeds_chunk_size():
    """累积不越界：每块都不超过 chunk_size，且块数按字符数算而非一段一块。"""
    text = "\n\n".join(f"第{i}段的内容" for i in range(200))
    out = chunk_text(text, chunk_size=60)
    assert all(len(c) <= 60 for c in out)
    assert 1 < len(out) < 200


def test_long_paragraph_is_not_merged_with_others():
    """整段就顶满 chunk_size 时单独成块，不与前后段落合并。"""
    long_para = "西" * 100
    assert chunk_text(f"{long_para}\n\n短段\n\n{long_para}", chunk_size=100) == \
           [long_para, "短段", long_para]


def test_fragmented_lines_merge_instead_of_one_chunk_per_line():
    """碎行合并成块：网页正文一行一个 DOM 文本节点，逐行成块会让块数放大两个数量级。"""
    text = "\n\n".join(f"导航项{i}" for i in range(8525))
    out = chunk_text(text, chunk_size=600, overlap=100)
    assert all(len(c) <= 600 for c in out)
    assert 50 < len(out) < 200


def test_long_paragraph_splits_on_sentence():
    """超长段落按句末标点切开，且句子内容不被破坏。"""
    sents = ["第一句讲西湖的玩法。", "第二句讲灵隐寺的时间！", "第三句讲避坑要点？"]
    text = "".join(sents)
    out = chunk_text(text, chunk_size=12)
    assert out == sents


def test_long_paragraph_sentences_merge_into_chunks():
    """降到句子一级后同样累积成块，不是一句一块。"""
    out = chunk_text("西湖很美。" * 100, chunk_size=60)
    assert all(len(c) <= 60 for c in out)
    assert len(out) < 20


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


# 快照测试：chunk_text 的输出是喂 embedding 的语料，带偏移的重构不许动它一个字。
# 期望值是重构前跑真实实现打印出来的（见 git 历史），不是照新实现猜的。
_CHUNK_SNAPSHOT_CASES = [
    ("段落一。\n\n段落二。", 600, 100, ["段落一。\n段落二。"]),
    ("甲。乙。丙。", 4, 1, ["甲。", "乙。", "丙。"]),
    ("a" * 12, 5, 2, ["aaaaa", "aaaaa", "aaaaa", "aaa"]),
    ("![]()\x00IMG0\x00尾。", 600, 100, ["![]()\x00IMG0\x00尾。"]),
]


def test_chunk_units_matches_snapshot():
    for text, size, overlap, expected in _CHUNK_SNAPSHOT_CASES:
        assert [c for c, _, _ in chunk_units(text, size, overlap)] == expected


def test_chunk_units_spans_point_at_the_chunk():
    """区间只用来判「图片起始偏移落在哪一块」，不要求块文本等于 text[s:e]：
    块内单元是用 "\\n" 拼起来的，原文里那些位置可能是别的空白。"""
    text = "段落一。\n\n段落二。"
    units = chunk_units(text, 600, 100)
    assert len(units) == 1
    _, s, e = units[0]
    assert text[s:e] == text             # 单块覆盖全文


def test_chunk_spans_are_ordered_and_disjoint():
    spans = chunk_spans("甲。乙。丙。丁。戊。", 4, 1)
    assert spans == sorted(spans)
    for (s1, e1), (s2, _) in zip(spans, spans[1:]):
        assert s1 <= s2 and e1 <= s2
