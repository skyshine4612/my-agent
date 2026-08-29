# app/core/sub_agent.py
# 通用 call_sub_agent 工具工厂：顶层与子 Agent 共用，把复杂任务委派给对应业务的专业子 Agent。
# 用 contextvars.ContextVar 记录嵌套深度（允许「父→子→孙」两层），超限拒绝，防止无限递归。
import contextvars

from app.core.prompts import load_prompt

# 子 Agent 调用深度计数器：contextvars 语义下每个异步 Task 独立计数，
# 只有链式递归（子 agent 再 call_sub_agent）才会递增，并发的兄弟子 Agent 互不影响。
_sub_call_depth = contextvars.ContextVar("_sub_call_depth", default=0)

# 最大嵌套层数：允许「父→子→孙」两层嵌套，第 3 层起拒绝（对齐 SonettoHere _MAX_SUB_CALL_DEPTH=2）。
_MAX_SUB_CALL_DEPTH = 2


def make_call_sub_agent(businesses: dict, llm, registry):
    """构造 call_sub_agent 工具函数。

    参数：
        businesses: 业务名 → Business 实例的映射；subagent_type 枚举与委派目标都从它取
        llm:        LLM 客户端，透传给子 Agent（子 Agent 复用同一模型客户端）
        registry:   ToolRegistry，子 Agent 跑自身工具时用它执行；闭包捕获后供 run_stream 使用
    返回：
        callable：async 工具函数 call_sub_agent(subagent_type, task, name="") -> dict
    """
    # 工具描述从 prompts/sub_agent.md 加载，写进 schema 引导 LLM 何时委派而非直接查工具
    description = load_prompt("sub_agent")

    async def call_sub_agent(subagent_type: str, task: str, name: str = "") -> dict:
        """把复杂任务交给对应业务的专业子 Agent 处理，返回其最终结果 dict。

        参数：
            subagent_type: 业务类型（枚举见 schema，动态取已注册业务名）
            task:          交给子 Agent 的完整任务描述
            name:          可选，子会话显示名
        返回：
            {"answer": 子 Agent 最终文本}
        """
        depth = _sub_call_depth.get()
        # 达到最大嵌套深度：拒绝继续委派，避免「子→孙→曾孙」无限下钻
        if depth >= _MAX_SUB_CALL_DEPTH:
            return {"answer": "子 Agent 嵌套已达上限"}
        # 进入子 Agent：先加深计数；用 try/finally 恢复原值（而非递减），
        # 这样即使子 Agent 抛异常，计数也能精确还原，不污染同 Task 后续其他调用。
        _sub_call_depth.set(depth + 1)
        try:
            business = businesses[subagent_type]
            sub = business.build_sub_agent(llm)
            # 隔离上下文：只传子 system(rules)+user(task)，历史为空；
            # on_event=None 表示子 Agent 内部工具调用不上报（前端只显示 call_sub_agent 一个气泡）。
            answer = await sub.run_stream(task, [], registry, on_event=None)
            return {"answer": answer}
        finally:
            # 无论成功/异常都还原父级深度
            _sub_call_depth.set(depth)

    call_sub_agent.__name__ = "call_sub_agent"
    # 把 description 与 parameters 挂到函数对象上，供调用方注册时生成 OpenAI 工具 schema；
    # subagent_type 的 enum 动态取自 businesses 的 keys，约束 LLM 只能委派给已注册业务。
    call_sub_agent.description = description
    call_sub_agent.parameters = {
        "type": "object",
        "properties": {
            "subagent_type": {"type": "string", "enum": list(businesses.keys())},
            "task": {"type": "string"},
            "name": {"type": "string"},
        },
        "required": ["subagent_type", "task"],
    }
    return call_sub_agent
