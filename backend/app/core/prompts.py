# app/core/prompts.py
# 提示词加载器：把散落在代码里的提示词集中到 prompts/ 目录下的 .md 文件，
# 供顶层 system prompt 渲染与各业务 rules 加载时按名字读取。
from datetime import datetime
from pathlib import Path

# prompts 目录位于 app/prompts/：本文件在 app/core/，往上两级即 app/
PROMPT_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """按名字读取 prompts/<name>.md 的全文并返回。

    参数：
        name: 提示词文件名（不含 .md 后缀），如 "system" / "grounding"。
    返回：
        文件文本；用 rstrip 去掉文件末尾换行，避免拼进 system prompt 时带入多余空行。
    """
    path = PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8").rstrip()


def today_hint() -> str:
    """生成「今天是 YYYY-MM-DD（周X）」提示，注入 system prompt 让 LLM 知道当前日期。

    关键：LLM 本身不知道今天几号，查询车票/机票/天气等需要日期的工具时，若无日期提示
    会从训练数据里编造过去日期（如 2024），导致数据源拒绝「早于今天」的查询。
    """
    now = datetime.now()
    weekdays = "一二三四五六日"
    return f"今天是 {now:%Y-%m-%d}（周{weekdays[now.weekday()]}）。"


def render_system_prompt(skill_directory: str, tool_grounding: str) -> str:
    """把「可用业务清单」与工具事实规范填进 system.md 的两个插槽，生成顶层 system prompt。

    参数：
        skill_directory: 可用业务清单文本（每业务一行 name + description），来自各 skill 的 frontmatter
        tool_grounding:  事实规范（严禁编造事实数据），来自 load_prompt("grounding")
    返回：
        替换掉 {skill_directory} 与 {tool_grounding} 插槽后的完整 system prompt。
    """
    template = load_prompt("system")
    # 用 str.replace 而非 str.format 替换插槽：system.md 除插槽外暂无花括号，
    # 但未来提示词里可能出现 JSON 花括号（如 critic 的 {"ok":...}），replace 更稳，不会误解析。
    return (template
            .replace("{skill_directory}", skill_directory)
            .replace("{tool_grounding}", tool_grounding))
