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

    # description（何时调）与 parameters（入参 schema，name 用 enum 约束只能取已注册业务）直接传给 register
    registry.register(
        "get_skill",
        "获取指定业务的规划规则正文（如旅行规划规则）。处理某业务任务前只需调用一次，规则正文加载后会保留在上下文中，不要重复调用。",
        {
            "type": "object",
            "properties": {"name": {"type": "string", "enum": skill_names, "description": "业务名"}},
            "required": ["name"],
        },
        get_skill,
        "读取业务规则",
    )
