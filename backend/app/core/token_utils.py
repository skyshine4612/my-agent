# app/core/token_utils.py
# token 计数工具：优先 tiktoken 真实计数（cl100k_base），失败回退启发式估算。
# 供工作记忆 / 历史预算控制共用；真实计数消除「中英 1/4 字符」启发式在混合文本上的偏差。

import logging
import threading

logger = logging.getLogger(__name__)

# tiktoken 编码器进程级单例：惰性加载一次后复用，避免每次计数重复构造。
# _encoder_loaded 区分「已尝试加载」与「尚未尝试」，None 表示加载失败（回退启发式）。
_encoder = None
_encoder_loaded = False
_lock = threading.Lock()


def _get_encoder():
    """惰性加载 tiktoken cl100k_base 编码器（进程级单例），失败返回 None。

    cl100k_base 词表内置于 tiktoken 包，加载不联网；Windows 有预编译 wheel。
    对 qwen 词表是近似（中文略高估），但方向是「预算更保守」——宁可早淘汰也不超模型上下文。
    """
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    with _lock:
        if not _encoder_loaded:
            try:
                import tiktoken
                _encoder = tiktoken.get_encoding("cl100k_base")
            except Exception as e:  # 未安装 / 加载失败：降级为启发式，不让预算控制成为单点故障
                logger.warning("tiktoken 不可用，token 计数回退启发式估算：%s", e)
                _encoder = None
            _encoder_loaded = True
    return _encoder


def _heuristic(text: str) -> int:
    """启发式估算兜底：中文（CJK 汉字）按约 1 字符/token，其余按约 4 字符/token。

    仅在 tiktoken 不可用时启用，作为最后防线保证计数永不抛错。
    """
    cjk = sum(1 for ch in text if '一' <= ch <= '鿿')
    return cjk + (len(text) - cjk) // 4


def estimate_tokens(text: str) -> int:
    """返回文本的 token 数：优先 tiktoken 真实计数，失败回退启发式估算。

    空文本返回 0；tiktoken 计数本身抛异常时（极端输入）也回退启发式，保证永不抛错。
    """
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except Exception as e:
            logger.warning("tiktoken 计数失败，回退启发式：%s", e)
    return _heuristic(text)