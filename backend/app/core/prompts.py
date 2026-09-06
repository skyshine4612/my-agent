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


def render_long_term_hint(long_term_facts: list[str]) -> str:
    """把长期记忆 facts 拼成「用户历史偏好」块的内容，用「；」分隔；空则返回空串（渲染为「无」）。"""
    return "；".join(long_term_facts) if long_term_facts else ""


def render_history_summary(summary: str, conv_id: str) -> str:
    """把短期记忆摘要拼成独立 system 消息的内容：标题 + 摘要正文 + 完整历史落盘提示；空则返回空串。

    文案外壳（「【历史对话摘要】」「[完整历史存到 …，可用 read_file 读]」）集中在这里，
    agent_service 只传 summary 与 conv_id；返回非空时作为一条独立 system 消息注入。
    """
    if not summary:
        return ""
    return f"【历史对话摘要】\n{summary}\n[完整历史存到 {conv_id}/_history.txt，可用 read_file 读]"


def render_system_prompt(skill_directory: str, tool_grounding: str,
                         today: str = "", long_term_hint: str = "") -> str:
    """把可用业务清单、工具事实规范、日期与用户偏好填进 system.md 的插槽，生成顶层 system prompt。

    参数：
        skill_directory: 可用业务清单文本（每业务一行 name + description），来自各 skill 的 frontmatter
        tool_grounding:  事实规范（严禁编造事实数据），来自 load_prompt("grounding")
        today:           日期提示（today_hint()），必填
        long_term_hint:  用户历史偏好（render_long_term_hint()），可空（空则显示「无」）
    返回：
        替换掉全部插槽后的完整 system prompt（历史摘要由 render_history_summary 独立成 system 消息，不进此模板）。
    """
    template = load_prompt("system")
    # 用 str.replace 而非 str.format 替换插槽：system.md 除插槽外暂无花括号，
    # 但未来提示词里可能出现 JSON 花括号（如 critic 的 {"ok":...}），replace 更稳，不会误解析。
    return (template
            .replace("{skill_directory}", skill_directory)
            .replace("{tool_grounding}", tool_grounding)
            .replace("{today}", today or "无")
            .replace("{long_term_hint}", long_term_hint or "无"))
