# app/domains/base.py
# Domain（功能域）抽象：业务通过 Domain 可插拔接入后端。
# 每个 Domain 声明自己执行所需的关键输入（required_inputs，供 Planner 澄清缺失项），
# 并负责向工具/Agent 注册表挂载本域能力。
from abc import ABC, abstractmethod


class Domain(ABC):
    """功能域抽象基类：一个 Domain 代表一类可插拔的业务能力（如旅行、天气）。

    子类需实现：
        - register_tools(registry, ds)：把数据源方法封装成工具注册进 ToolRegistry
        - register_agents(registry)：注册本域子 Agent
    build() 提供一站式装配入口，一次拿到装配好的工具/Agent 注册表。
    """

    name: str
    required_inputs: list[str] = []   # 该功能执行所需的关键输入，Planner 据此澄清（缺则追问）

    @abstractmethod
    def register_tools(self, registry, ds):
        """把本域工具注册到 ToolRegistry：registry 负责注册，ds 为数据源。"""
        ...

    @abstractmethod
    def register_agents(self, registry):
        """把本域子 Agent 注册到 AgentRegistry。"""
        ...

    def build(self, ds):
        """一站式装配：创建工具/Agent 注册表，按子类实现填充后一并返回。"""
        from app.core.registry import ToolRegistry, AgentRegistry
        tools, agents = ToolRegistry(), AgentRegistry()
        self.register_tools(tools, ds)
        self.register_agents(agents)
        return tools, agents
