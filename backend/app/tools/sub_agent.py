# app/tools/sub_agent.py
"""call_sub_agent 工具：把复杂子任务委派给独立上下文的子 Agent，返回最终答案。

核心价值是「上下文隔离」：子 Agent 历史为空、只吃 task，跑完只回传答案，
主 Agent 的上下文不被几十万字的原始工具结果淹没。

工具结果地址索引按子任务隔离：每个子 Agent 分配独立的 sub_conversation_id（uuid），
子任务的超长结果写在这个子会话目录下，不与主会话、其他子 Agent 串扰；任务结束即清理。

子 Agent 与主 Agent 的差别只有两处：历史为空、工具集不含 call_sub_agent 自己（一层委派）。
事实校验（critic）与并发原子性都照旧——子 Agent 查到的硬数据由它自己的 critic 校验，
否则「委派出去的硬数据」就成了防编造机制的唯一缺口。
"""
import uuid
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.agent import Agent
from app.core.context import current_emit, current_tool_call_id, current_user_id
from app.core.prompts import load_prompt, today_hint


def make_call_sub_agent(model, registry, skill_names, context_budget, tool_result_store=None,
                        critic=None, max_tool_rounds=20):
    """构造 call_sub_agent 工具（langchain tool 对象）。

    子 Agent = 通用子助手：system prompt 含职责 + 可用业务清单 + 事实规范 + 日期；
    工具 = get_skill + 业务工具（排除 call_sub_agent 自己，避免递归委派）；
    规则由子 Agent 按需 get_skill 加载，主 Agent 委派时不指定 skill。
    tool_result_store：工具结果地址索引存储，子 Agent 超长结果同样落库（与主 Agent 统一）；
    为 None 时不落库（保持向后兼容）。
    max_tool_rounds：子 Agent 自己的工具调用轮次上限（与主 Agent 各自计数）。
    model：langchain 模型实例（子 Agent 的 ReAct 循环与工作记忆蒸馏都由它驱动）。
    critic：CriticConfig，非空时子 Agent 也做事实校验。子 Agent 调硬数据工具时，主 Agent 的
    hard_results 是空的（它这轮调的是 call_sub_agent，不在 HARD_DATA_TOOLS 里），
    主 Agent 的 critic 因此不会触发；校验必须落在子 Agent 自己这一轮，否则委派即绕过校验。
    """

    @tool
    async def call_sub_agent(
        title: Annotated[str, Field(description="这次委派的短标题，8 字以内，用来在界面上区分并行的子任务，如「经典景点游」")],
        task: Annotated[str, Field(description="交给子助手的完整子任务描述")],
    ) -> str:
        """把整块独立工作委派给独立上下文的子助手，只带回结论，它的中间过程（多次工具调用与大量原始结果）不进你的上下文。

USE FOR（三条同时满足才用）：互不相干的整块工作；每块自己就要多轮查询才做得完；你只需要它的结论、不需要原始字段。
典型场景：一次规划多个目的地（每个目的地一个子任务）；用户要 2-3 套不同侧重点的方案（每套方案一个子任务）。
不要把同一个目的地内部的交通/天气/景点/住宿/美食拆成多个子任务——那种情况在同一轮并行调用对应工具即可，委派只会更慢更贵。

RULES：
- 子助手对你的对话一无所知，也没看过用户的原话。task 里必须写全：目的地、日期、人数与偏好、以及你要求它按什么格式返回。
- 子助手只回一段文字结论。要它返回能直接拼进你输出模板的片段（如「按 天气/景点/美食 三段返回」），不要让它写散文给你转述。
- 共享的事实（如全程的天气、城际交通）你自己查一次，别让每个子任务各查一遍。
- 子助手的结论是它的自述，关键数字（车次、票价、开放时间）以它自己的工具结果为据；你拿不准就自己复校。"""
        # title 在函数体里刻意不用：它只为让模型产出一个能区分并行子任务的短标签，
        # 随 tool_call 事件的 args 一起下发并落库，前端直接读 args.title 当节点名。
        # 改成可选或删掉，界面上就会出现几个分不清谁是谁的「委派子任务」。
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
        base_emit = current_emit.get()
        # 本 call 的 tool_call_id = 这次委派的身份：子 Agent 的每个事件都带上 parent=它，
        # 前端据此把「哪个子任务在干什么」挂到发起它的那个委派气泡下（并行委派时靠它区分）
        parent_id = current_tool_call_id.get()

        async def child_emit(event):
            """给子 Agent 的事件盖上归属章再转发：parent 是委派 id，agent 是层级。"""
            event["parent"] = parent_id
            if base_emit is not None:
                await base_emit(event)

        try:
            sub = Agent(name="sub", system_prompt=sub_system, tools=sub_tools, model=model)
            # 隔离上下文：历史为空，只传 task；on_event 转发子 Agent 的工具调用与阶段状态，
            # 沿同一条 SSE 通道上报（每条都带 parent，前端挂到对应委派气泡下）
            answer, _ = await sub.run_stream(task, [], registry, on_event=child_emit,
                                             conversation_id=sub_conversation_id,
                                             context_budget=context_budget,
                                             tool_result_store=tool_result_store,
                                             critic=critic,
                                             max_tool_rounds=max_tool_rounds)
            # 回纯文本：返回 dict 会被父 Agent 侧的 str() 转成 repr，换行变字面量 \n、整段挤成一行
            return answer
        finally:
            # 子任务结束（含异常）清理子会话工具结果地址索引，不留垃圾记录
            if tool_result_store is not None:
                await tool_result_store.delete_conversation(sub_conversation_id, user_id)

    return call_sub_agent
