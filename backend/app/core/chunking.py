# app/core/chunking.py
# 文本切块：递归切分 + 重叠，从粗到细降级（段落 → 句子 → 字符硬切）。
# 纯函数，便于单测；按字符数而非 token 数计量，对中文更直观。
import re

# 句末标点：中英文都要，否则英文攻略整段切不开
_SENTENCE_END = r"(?<=[。！？!?])"


def chunk_text(text: str, chunk_size: int = 600, overlap: int = 100) -> list[str]:
    """把长文本切成不超过 chunk_size 的块；仅字符硬切那一级保留 overlap 重叠。

    边界优先级：段落（空行）→ 句子（句末标点）→ 字符硬切。
    段落通常是一个完整意思（「必去景点」一段、「避坑」一段），所以优先按它切。
    """
    chunks: list[str] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            chunks.append(para)
            continue

        for sent in re.split(_SENTENCE_END, para):
            if not sent.strip():
                continue
            if len(sent) <= chunk_size:
                chunks.append(sent)
                continue
            # 单句仍超长（通篇无标点的长串）：只能硬切，靠 overlap 弥补被切断的语义边界
            step = max(1, chunk_size - overlap)
            for i in range(0, len(sent), step):
                piece = sent[i:i + chunk_size]
                if piece.strip():
                    chunks.append(piece)
    return chunks
