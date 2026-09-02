# app/core/token_utils.py
# token 估算工具：中英文混排文本的粗略 token 数估算，供工作记忆 / 历史预算控制共用。


def estimate_tokens(text: str) -> int:
    """估算文本的 token 数：中文（CJK 汉字）按约 1 字符/token，其余按约 4 字符/token。

    中文 tokenizer 的压缩率远低于英文（1 个汉字 ≈ 1 个 token），
    若统一按「4 字符/token」会严重低估中文、导致预算控制失效，故区分中英分别估算。
    """
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    return cjk + (len(text) - cjk) // 4