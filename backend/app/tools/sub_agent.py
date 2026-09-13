# app/tools/sub_agent.py
"""call_sub_agent 工具：把复杂子任务委派给独立上下文的子 Agent，返回最终答案摘要。

核心价值是「上下文隔离」：子 Agent 历史为空、只吃 task，跑完只回传摘要，
主 Agent 的上下文不被几十万字的原始工具结果淹没。

工具结果地址索引按子任务隔离：每个子 Agent 分配独立的 sub_conversation_id（uuid），
子任务的超长结果写在这个子会话目录下，不与主会话、其他子 Agent 串扰；任务结束即清理。
"""
import uuid
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.agent import Agent
from app.core.context import current_user_id
from app.core.prompts import load_prompt, today_hint


def make_call_sub_agent(model, registry, skill_names, context_budget, tool_result_store=None):
    """构造 call_sub_agent 工具（langchain tool 对象）。

    子 Agent = 通用子助手：system prompt 含职责 + 可用业务清单 + 事实规范 + 日期；
    工具 = get_skill + 业务工具（排除 call_sub_agent 自己，避免递归委派）；
    规则由子 Agent 按需 get_skill 加载，主 Agent 委派时不指定 skill。
    tool_result_store：工具结果地址索引存储，子 Agent 超长结果同样落库（与主 Agent 统一）；
    为 None 时不落库（保持向后兼容）。
    model：langchain 模型实例（子 Agent 的 ReAct 循环与工作记忆蒸馏都由它驱动）。
    """

    @tool
    async def call_sub_agent(
        task: Annotated[str, Field(description="交给子助手的完整子任务描述")],
    ) -> dict:
        """把独立且过程长的子任务委派给独立上下文的子助手，只带回结论摘要，避免它的中间过程（多次工具调用与大量原始结果）占据主上下文。适用于互不相干、且各自都需要多轮查询才能答全的子任务；单个对象、或一次查询就能答的，直接调对应工具即可，委派只会更慢更贵。"""
        skill_directory = "\n".join(f"- {n}" for n in skill_names)
        sub_system = (
            "你是子助手，独立完成主助手委派给你的子任务，只返回最终结果，不要复述过程。\n"
            f"可用的业务：\n{skill_directory}\n"
            f"{load_prompt('grounding')}\n\n"
            f"{today_hint()}"
        )
        # 子 Agent 的工具：全部工具排除 call_sub_agent 自身（一层委派，避免无限下钻）
        sub_tools = [t for t in registry.list_names() if t != "call_sub_agent"]
        # 子会话 id：独立 uuid，工具结果地址索引按子任务隔离，不混入主会话、不与其他子 Agent 冲突
        sub_conversation_id = uuid.uuid4().hex
        user_id = current_user_id.get()
        try:
            sub = Agent(name="sub", system_prompt=sub_system, tools=sub_tools, model=model)
            # 隔离上下文：历史为空，只传 task；on_event=None 子 Agent 内部工具调用不上报前端
            answer, _ = await sub.run_stream(task, [], registry, on_event=None,
                                             conversation_id=sub_conversation_id,
                                             context_budget=context_budget,
                                             tool_result_store=tool_result_store)
            return {"answer": answer}
        finally:
            # 子任务结束（含异常）清理子会话工具结果地址索引，不留垃圾记录
            if tool_result_store is not None:
                await tool_result_store.delete_conversation(sub_conversation_id, user_id)

    return call_sub_agent
