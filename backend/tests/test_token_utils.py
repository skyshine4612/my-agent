# tests/test_token_utils.py
# token 计数契约测试：验证真实计数（tiktoken）与启发式回退的行为契约，不绑定内部实现。
from app.core.token_utils import estimate_tokens


def test_estimate_tokens_empty_is_zero():
    """空文本返回 0。"""
    assert estimate_tokens("") == 0


def test_estimate_tokens_positive_and_monotonic():
    """非空文本返回正值，且更长文本 token 数更大（真实计数/启发式都满足）。"""
    short = estimate_tokens("你好")
    long = estimate_tokens("你好世界，这是一个更长的句子")
    assert short > 0
    assert long > short


def test_estimate_tokens_mixed_content_no_error():
    """中英数字混合内容计数不抛错、返回正值。"""
    assert estimate_tokens("hello 123 中文 mixed") > 0
