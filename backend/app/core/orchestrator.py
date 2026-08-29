# app/core/orchestrator.py
# 编排器（Orchestrator）：把 Planner 拆解出的子任务 DAG 按拓扑分层，
# 层内用 asyncio.gather 并行调度各子 Agent，中间结果卸载到 TaskMemory（工作记忆卸载）。
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SubTask:
    """一个可调度的子任务：目标 Agent + 参数 + 依赖"""
    id: str
    agent: str
    params: dict = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)


def topo_layers(tasks):
    """拓扑分层：同一层无依赖可并行，返回 [[层1任务],[层2任务],...]"""
    layers, done = [], set()
    # 循环直到所有子任务都被分入某一层
    while len(done) < len(tasks):
        # 本轮可用任务 = 尚未分层、且其所有依赖都已在之前层完成
        layer = [t for t in tasks if t.id not in done and all(d in done for d in t.depends_on)]
        # 无任务可调度却仍有剩余任务 → 存在循环依赖，直接报错
        if not layer:
            raise ValueError("子任务存在循环依赖")
        layers.append(layer); done |= {t.id for t in layer}
    return layers


class Orchestrator:
    """编排器：按 DAG 拓扑分层，层内 asyncio.gather 并行调度各子 Agent，结果卸载到 TaskMemory"""
    def __init__(self, agent_registry, task_memory):
        self.agents = agent_registry; self.task_memory = task_memory

    async def execute(self, subtasks, history, tool_registry, task_id=None, on_step=None):
        """按拓扑分层执行子任务 DAG：每层并行、层间顺序，逐子任务卸载结果，返回 {id: output}。

        task_id：真实的任务运行 id（由 TaskMemory.create_task 生成），卸载时用它作为
        task_id、子任务 id 作为 subtask_id 分开记录；未传时兜底复用子任务 id，兼容无落库的调用方。
        on_step：透传给子 Agent 的步骤回调（把 ReAct 中间步骤上报出去，供 SSE 推给前端）。
        """
        results = {}
        for layer in topo_layers(subtasks):
            # 日志：记录本层将并行调度的子 Agent（同一层无依赖，可并行）
            logger.info("[编排] 并行执行本层子任务：%s", [t.agent for t in layer])
            async def run_one(t):
                agent = self.agents.get(t.agent)
                out = await agent.run(f"任务参数：{t.params}", history, tool_registry, on_step=on_step)
                # 卸载中间结果到任务记忆（工作记忆只保留结果，不膨胀上下文）
                # 用真实 task_id + 子任务 id 记录，避免二者混用导致结果读取错位
                await self.task_memory.save_subtask_result(task_id or t.id, t.id, t.agent, str(t.params), out)
                # 实时通知子任务完成（供 SSE 实时推给前端，避免等到全部执行完才看到状态变化）
                if on_step:
                    await on_step({"type": "task_done", "task_id": t.id})
                return t.id, out
            # 层内并行执行无依赖的子任务
            for tid, out in await asyncio.gather(*(run_one(t) for t in layer)):
                results[tid] = out
                # 日志：记录子任务完成与结果摘要
                logger.info("[编排] 子任务 %s 完成，结果 %.80s", tid, out)
        return results
