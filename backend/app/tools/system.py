# app/tools/system.py
# 系统工具：get_skill（按需加载业务规则正文）。
# 工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
# name 的 enum 约束用动态 Literal 承载（清单为空时退化为 str，见下）：业务清单是启动时扫描 skills/ 得到的，不可硬编码字面量。
from typing import Annotated, Literal

from langchain_core.tools import tool
from pydantic import Field

from app.core.skills import get_skill_body


def register_system_tools(registry, skill_names):
    """把系统工具注入 ToolRegistry。

    参数：
        registry:    ToolRegistry，工具注册表
        skill_names: 业务名列表（get_skill 的 name 参数 enum，约束 LLM 只能取已注册业务）
    """
    if skill_names:
        SkillName = Literal[tuple(skill_names)]
    else:
        # 空清单时 Literal[()] 会让 pydantic 建模抛 AssertionError，而 service 在 import 期构造，等于启动崩溃。
        # 退化为 str：无 enum 约束，模型传任何值都落到下面「未知业务」分支，与旧 schema 的 enum:[] 一样不可用于选值。
        SkillName = str

    @tool
    async def get_skill(
        name: Annotated[SkillName, Field(description="业务名")],
    ) -> str:
        """获取指定业务的规划规则正文（如旅行规划规则）。处理某业务任务前只需调用一次，规则正文加载后会保留在上下文中，不要重复调用。"""
        body = get_skill_body(name)
        return body if body else f"未知业务：{name}"

    registry.register_tool(get_skill, "读取业务规则")
