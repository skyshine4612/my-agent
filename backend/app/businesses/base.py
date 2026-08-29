# app/businesses/base.py
# Business（业务）抽象：每个业务代表一类可插拔能力（如旅行），
# 声明自身规则（子 agent system prompt 主体）与工具白名单，并提供装配子 Agent 的默认实现。
from abc import ABC, abstractmethod

from app.core.agent import Agent
from app.core.registry import ToolRegistry


class Business(ABC):
    """业务抽象基类：代表一类可插拔业务能力，供顶层 system prompt 生成业务目录、call_sub_agent 委派。

    属性：
        name:        业务名（唯一键，进业务目录与 call_sub_agent 的 enum）
        description: 一行描述（进顶层业务目录，帮助 LLM 选择委派目标）
        rules:       业务规则文本（子 agent system prompt 主体）
        tool_names:  本业务可调用的工具白名单
    子类需实现 register_tools 把本业务工具注入 ToolRegistry；
    build_sub_agent 提供默认装配：system prompt = rules，tools = tool_names + ["call_sub_agent"]。
    """
    name: str
    description: str
    rules: str
    tool_names: list[str]

    @abstractmethod
    def register_tools(self, registry: ToolRegistry, deps) -> None:
        """把本业务工具注册到 ToolRegistry。

        参数：
            registry: 工具注册表，本业务工具都挂到它上面
            deps:     依赖对象（如 AmapMcpDataSource），由调用方注入
        """
        ...

    def build_sub_agent(self, llm) -> Agent:
        """构造本业务的子 Agent：system prompt = rules，工具 = tool_names + ["call_sub_agent"]。

        子 Agent 额外带上 call_sub_agent 工具，允许其继续把子任务委派给其他业务（受深度上限约束）。
        """
        return Agent(name=self.name, system_prompt=self.rules,
                     tools=self.tool_names + ["call_sub_agent"], llm=llm)
