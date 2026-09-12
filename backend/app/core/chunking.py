# app/core/chunking.py
# 文本切块：单元按顺序累积到 chunk_size 再成块，单元自身超长则逐级降级
# （段落 → 句子 → 字符硬切）。纯函数，便于单测；按字符数而非 token 数计量，对中文更直观。
#
# 单元在产出时一并带上它在原文里的偏移：图片归属（ingest 的偏移法）要按「图片起始位置
# 落在哪一块」判定，而块是单元的累积 —— 只有单元这一层还知道自己在原文里的位置。
import re


def _paragraph_spans(text: str):
    """按 \\n\\n 切段并给出每段的偏移（切法与 str.split("\\n\\n") 完全一致）。"""
    start = 0
    for part in text.split("\n\n"):
        yield start, start + len(part)
        start += len(part) + 2


def _sentence_spans(seg: str):
    """按句末标点切句并给出偏移（等价于 re.split(r"(?<=[。！？!?])", seg)）。"""
    start = 0
    for m in re.finditer(r"[。！？!?]", seg):
        yield start, m.end()
        start = m.end()
    if start < len(seg):
        yield start, len(seg)


def _units(text: str, chunk_size: int, overlap: int):
    """产出切块的最小单元 (单元文本, 起始偏移, 结束偏移)，每个单元自身不超过 chunk_size。

    降级顺序：段落；超长段落降到句子；单句仍超长（通篇无标点的长串）只能字符硬切，
    靠 overlap 弥补被切断的语义边界。
    """
    for p_start, p_end in _paragraph_spans(text):
        raw = text[p_start:p_end]
        para = raw.strip()
        if not para:
            continue
        # 段落被 strip 掉了首尾空白，偏移要跟着缩进去，否则图片会挂到相邻块上
        start = p_start + (len(raw) - len(raw.lstrip()))
        end = start + len(para)
        if len(para) <= chunk_size:
            yield para, start, end
            continue

        for s_off, e_off in _sentence_spans(text[start:end]):
            s, e = start + s_off, start + e_off
            sent = text[s:e]
            if not sent.strip():
                continue
            if len(sent) <= chunk_size:
                yield sent, s, e
                continue
            step = max(1, chunk_size - overlap)
            for i in range(0, e - s, step):
                piece = text[s + i:min(s + i + chunk_size, e)]
                if piece.strip():
                    yield piece, s + i, s + i + len(piece)


def chunk_units(text: str, chunk_size: int = 600, overlap: int = 100):
    """切块，返回 [(块文本, 起始偏移, 结束偏移)]。

    块文本与 `chunk_text` 逐字对应（同一份累积逻辑）；偏移是该块在原文里覆盖到的范围
    （首个单元的起点 → 末个单元的终点）。单元之间拼接用的 "\\n" 不在原文里，
    所以块文本未必等于 text[起:止]——偏移只用来判归属，不用来切片。
    """
    out: list[tuple[str, int, int]] = []
    buf = ""
    start = end = 0
    for unit, s, e in _units(text, chunk_size, overlap):
        if buf and len(buf) + 1 + len(unit) > chunk_size:
            out.append((buf, start, end))
            buf = ""
        if buf:
            buf = f"{buf}\n{unit}"
        else:
            buf, start = unit, s
        end = e
    if buf:
        out.append((buf, start, end))
    return out


def chunk_spans(text: str, chunk_size: int = 600, overlap: int = 100) -> list[tuple[int, int]]:
    """每块在原文里的区间 [(start, end)]，顺序与 `chunk_text` 一一对应。"""
    return [(s, e) for _, s, e in chunk_units(text, chunk_size, overlap)]


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    """把长文本切成不超过 chunk_size 的块；仅字符硬切那一级保留 overlap 重叠。

    单元累积到「再加一个就超 chunk_size」时成块，块内单元用换行分隔。碎行因此会与相邻行
    合并：网页正文是一行一个 DOM 文本节点，逐行成块会把块数放大两个数量级（embedding
    调用数同比例上涨），单块字符太少也带不动语义检索。
    """
    return [c for c, _, _ in chunk_units(text, chunk_size, overlap)]
