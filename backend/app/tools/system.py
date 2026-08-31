# app/tools/system.py
# 系统工具：get_skill（按需加载业务规则正文）。
from app.core.skills import get_skill_body


def register_system_tools(registry, skill_names):
    """把系统工具注入 ToolRegistry。

    参数：
        registry:    ToolRegistry，工具注册表
        skill_names: 业务名列表（get_skill 的 name 参数 enum，约束 LLM 只能取已注册业务）
    """

    async def get_skill(name: str) -> str:
        """按业务名返回对应 skill 的规划规则正文；未知业务返回提示。"""
        body = get_skill_body(name)
        return body if body else f"未知业务：{name}"

    get_skill.__name__ = "get_skill"
    get_skill.description = "获取指定业务的规划规则正文（如旅行规划规则）。处理业务任务前先调用，按规则执行。"
    get_skill.parameters = {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": skill_names, "description": "业务名"}},
        "required": ["name"],
    }
    registry.register("get_skill", get_skill.description, get_skill.parameters, get_skill, "读取业务规则")
