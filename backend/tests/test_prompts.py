# tests/test_prompts.py
# 提示词加载器契约测试：load_prompt 读 .md 全文、render_system_prompt 正确替换两个插槽。
from app.core.prompts import load_prompt, render_system_prompt


def test_load_prompt_reads_markdown():
    """load_prompt 能按名字读到 prompts/<name>.md 的文本内容。"""
    travel = load_prompt("travel")
    assert isinstance(travel, str) and travel.strip()
    assert "旅行规划专家" in travel
    # system.md 保留两个待填插槽，由 render_system_prompt 替换
    system = load_prompt("system")
    assert "{business_directory}" in system and "{tool_grounding}" in system


def test_render_system_prompt_fills_slots():
    """render_system_prompt 把业务目录与事实规范填进两个插槽，输出不再含占位符。"""
    directory = "- travel：旅行规划（行程/交通/景点/天气/预算）"
    grounding = load_prompt("grounding")
    out = render_system_prompt(directory, grounding)
    assert "{business_directory}" not in out
    assert "{tool_grounding}" not in out
    assert directory in out
    assert grounding in out
    assert "你是通用多业务助手" in out
