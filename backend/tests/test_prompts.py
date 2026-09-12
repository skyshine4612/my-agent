# tests/test_prompts.py
# 提示词加载器契约测试：load_prompt 读 .md 全文、render_system_prompt 正确替换各插槽。
from datetime import datetime

from app.core.prompts import (load_prompt, render_history_summary, render_long_term_hint,
                              render_system_prompt, today_hint)


def test_today_hint_contains_current_date():
    """today_hint 的输出必须含当天日期（YYYY-MM-DD）与周几，让 LLM 不再编造过去日期。"""
    hint = today_hint()
    # 包含今天日期（格式 YYYY-MM-DD）
    assert datetime.now().strftime("%Y-%m-%d") in hint
    # 含周几提示（周一 ~ 周日 之一）
    assert any(f"周{wd}" in hint for wd in "一二三四五六日")


def test_load_prompt_reads_markdown():
    """load_prompt 能按名字读到 prompts/<name>.md 的文本内容。"""
    grounding = load_prompt("grounding")
    assert isinstance(grounding, str) and grounding.strip()
    # system.md 保留两个待填插槽，由 render_system_prompt 替换
    system = load_prompt("system")
    assert "{skill_directory}" in system and "{tool_grounding}" in system


def test_render_system_prompt_fills_slots():
    """render_system_prompt 把业务清单、事实规范、日期与偏好填进插槽，输出不再含占位符。"""
    directory = "- travel：旅行规划（行程/交通/景点/天气/预算）"
    grounding = load_prompt("grounding")
    out = render_system_prompt(directory, grounding,
                               today="今天是 2026-09-06（周六）。",
                               long_term_hint="喜欢自然风光；偏好经济型酒店")
    assert "{skill_directory}" not in out
    assert "{tool_grounding}" not in out
    assert "{today}" not in out
    assert "{long_term_hint}" not in out
    assert directory in out
    assert grounding in out
    assert "今天是 2026-09-06（周六）。" in out
    assert "喜欢自然风光；偏好经济型酒店" in out


def test_render_long_term_hint():
    """render_long_term_hint 用「；」连接 facts，空则返回空串。"""
    assert render_long_term_hint([]) == ""
    assert render_long_term_hint(["喜欢自然风光", "偏好经济型酒店"]) == "喜欢自然风光；偏好经济型酒店"


def test_render_history_summary():
    """render_history_summary 拼标题 + 摘要正文 + 完整历史落盘提示，空则返回空串。"""
    assert render_history_summary("", "c1") == ""
    out = render_history_summary("第一天去了武侯祠", "c1")
    assert "【历史对话摘要】" in out
    assert "第一天去了武侯祠" in out
    assert "c1/_history.txt" in out
    assert "read_file" in out


def test_render_system_prompt_empty_context_shows_none():
    """today / long_term_hint 为空时降级成「无」，而不是留一个空标题或未替换的占位符。"""
    out = render_system_prompt("- travel：旅行规划", load_prompt("grounding"))
    assert "无" in out
    for slot in ("{today}", "{long_term_hint}", "{skill_directory}", "{tool_grounding}"):
        assert slot not in out
