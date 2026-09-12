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


def _format_size(size: int) -> str:
    """小于 1MB 的按 KB 显示：5788 字节渲染成 0.0MB，模型对「哪个文件更大」失去判断。"""
    return f"{size / 1048576:.1f}MB" if size >= 1048576 else f"{size / 1024:.1f}KB"


def render_file_directory(entries: dict[str, dict]) -> str:
    """把会话文件清单渲染成「名称 + 大小 + 来源标注」的列表文本，供 list_uploaded_files 返回。

    入参是「文件名 → 条目」的映射（`FileStore.list_by_name`），不是文件列表：同名只留最新这条
    规则在那一个地方收口，这里不再自己判重 —— 清单里出现的名字，必须就是模型报回来时能解析到
    的那个文件，两处各去一次重就可能对不上。

    来源标注是必要的——模型得知道哪个是用户给的、哪个是自己刚生成的。
    """
    lines = []
    for name, f in entries.items():
        source = f.get("source", "upload")
        note = "" if source == "upload" else f"（由 {source.removeprefix('script:')} 生成）"
        lines.append(f"- {name}  {_format_size(f['size'])}{note}")
    return "\n".join(lines)


def render_turn_files(files: list[dict]) -> str:
    """把本条消息携带的文件渲染成「本条消息携带的文件」块。

    单独成块而非混进会话清单：本轮要处理谁必须是**事实**，不能让模型从整会话清单里猜。
    清单是历史（含脚本产物），只在用户点名「上一个 / 之前那个」时才用。
    """
    return "\n".join(f"- {f['name']}  {_format_size(f.get('size', 0))}" for f in files)


def render_user_message(message: str, files: list[dict]) -> str:
    """把用户消息与随消息上传的附件合成喂给 LLM 的 user 正文；无附件时原样返回。

    system prompt 的「本轮上传件」块每轮重建，下一轮就没了；附件写进 user 正文才随短期记忆留存，
    用户下一轮说「我上次传的那个文件」时模型才有事实可依，不必去会话清单里猜。
    """
    if not files:
        return message
    return f"{message}\n[本条消息携带的文件]\n{render_turn_files(files)}"


def render_system_prompt(skill_directory: str, tool_grounding: str,
                         today: str = "", long_term_hint: str = "") -> str:
    """把可用业务清单、事实规范、日期与用户偏好填进 system.md 的插槽。

    参数：
        skill_directory: 可用业务清单文本（每业务一行 name + description），来自各 skill 的 frontmatter
        tool_grounding:  事实规范（严禁编造事实数据），来自 load_prompt("grounding")
        today:           日期提示（today_hint()），必填
        long_term_hint:  用户历史偏好（render_long_term_hint()），可空（空则显示「无」）

    文件不进这里：本轮携带的附件折进 user 正文（render_user_message），会话里还有哪些文件由
    list_uploaded_files 工具按需查。放 system 等于每轮重发一份随时在变的清单，且只对本轮有效。
    """
    template = load_prompt("system")
    # 用 str.replace 而非 str.format 替换插槽：提示词正文里若有 JSON 花括号（如 critic 的 {"ok":...}），
    # format 会把它当占位符解析，replace 不会。
    return (template
            .replace("{skill_directory}", skill_directory)
            .replace("{tool_grounding}", tool_grounding)
            .replace("{today}", today or "无")
            .replace("{long_term_hint}", long_term_hint or "无"))
