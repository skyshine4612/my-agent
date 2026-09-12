# app/core/chunking.py
# 文本切块：单元按顺序累积到 chunk_size 再成块，单元自身超长则逐级降级
# （段落 → 句子 → 字符硬切）。纯函数，便于单测；按字符数而非 token 数计量，对中文更直观。
import re

# 句末标点：中英文都要，否则英文攻略整段切不开
_SENTENCE_END = r"(?<=[。！？!?])"


def _units(text: str, chunk_size: int, overlap: int):
    """产出切块的最小单元，每个单元自身不超过 chunk_size。

    降级顺序：段落；超长段落降到句子；单句仍超长（通篇无标点的长串）只能字符硬切，
    靠 overlap 弥补被切断的语义边界。
    """
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            yield para
            continue

        for sent in re.split(_SENTENCE_END, para):
            if not sent.strip():
                continue
            if len(sent) <= chunk_size:
                yield sent
                continue
            step = max(1, chunk_size - overlap)
            for i in range(0, len(sent), step):
                piece = sent[i:i + chunk_size]
                if piece.strip():
                    yield piece


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    """把长文本切成不超过 chunk_size 的块；仅字符硬切那一级保留 overlap 重叠。

    单元累积到「再加一个就超 chunk_size」时成块，块内单元用换行分隔。碎行因此会与相邻行
    合并：网页正文是一行一个 DOM 文本节点，逐行成块会把块数放大两个数量级（embedding
    调用数同比例上涨），单块字符太少也带不动语义检索。
    """
    chunks: list[str] = []
    buf = ""
    for unit in _units(text, chunk_size, overlap):
        if buf and len(buf) + 1 + len(unit) > chunk_size:
            chunks.append(buf)
            buf = ""
        buf = f"{buf}\n{unit}" if buf else unit
    if buf:
        chunks.append(buf)
    return chunks
