# app/tools/sub_agent.py
"""call_sub_agent 工具：把复杂子任务委派给独立上下文的子 Agent，返回最终答案摘要。

核心价值是「上下文隔离」：子 Agent 历史为空、只吃 task，跑完只回传摘要，
主 Agent 的上下文不被几十万字的原始工具结果淹没。
"""
from app.core.agent import Agent
from app.core.prompts import load_prompt, today_hint


def make_call_sub_agent(llm, registry, skill_names):
    """构造 call_sub_agent 工具函数。

    子 Agent = 通用子助手：system prompt 含职责 + 可用业务清单 + 事实规范 + 日期；
    工具 = get_skill + 业务工具（排除 call_sub_agent 自己，避免递归委派）；
    规则由子 Agent 按需 get_skill 加载，主 Agent 委派时不指定 skill。
    """
    async def call_sub_agent(task: str) -> dict:
        """把复杂子任务交给独立上下文的子助手，返回其最终答案摘要。"""
        skill_directory = "\n".join(f"- {n}" for n in skill_names)
        sub_system = (
            "你是子助手，独立完成主助手委派给你的子任务，只返回最终结果，不要复述过程。\n"
            f"可用的业务：\n{skill_directory}\n"
            "处理业务任务时，先调用 get_skill 获取对应业务的规则，再按规则调用工具。\n\n"
            f"{load_prompt('grounding')}\n\n"
            f"{today_hint()}"
        )
        # 子 Agent 的工具：全部工具排除 call_sub_agent 自身（一层委派，避免无限下钻）
        sub_tools = [t for t in registry.list_names() if t != "call_sub_agent"]
        sub = Agent(name="sub", system_prompt=sub_system, tools=sub_tools, llm=llm)
        # 隔离上下文：历史为空，只传 task；on_event=None 子 Agent 内部工具调用不上报前端
        answer = await sub.run_stream(task, [], registry, on_event=None)
        return {"answer": answer}

    call_sub_agent.__name__ = "call_sub_agent"
    call_sub_agent.description = (
        "把相互独立的子任务（如查交通、查景点、查美食）委派给独立上下文的子助手执行，"
        "子助手会自行调用工具并按业务规则处理，最终只返回结果摘要。"
        "当本轮需要多个相互独立的查询、或某个查询会返回大量数据（如几十个车次）时，"
        "应把这类子任务委派出去，避免主助手的上下文被大量工具结果淹没。"
    )
    call_sub_agent.parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "交给子助手的完整子任务描述"},
        },
        "required": ["task"],
    }
    return call_sub_agent
