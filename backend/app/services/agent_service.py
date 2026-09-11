# app/services/agent_service.py
# 多业务通用 service：把会话历史、短期记忆、长期记忆、LLM、数据源、业务工具
# 串成一个完整的 AgentService，对外提供 SSE 流式对话 chat_stream；记忆编排委托给 core/memory/manager.py。
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from app.config import settings
from app.core.agent import Agent
from app.core.context import current_user_id
from app.core.critic import run_critic
from app.core.knowledge import KnowledgeStore
from app.core.llm import get_llm
from app.core.memory import ConversationStore, LongTermMemory, ShortTermMemory, ToolResultStore
from app.core.memory.manager import MemoryManager
from app.core.prompts import (load_prompt, render_history_summary, render_long_term_hint,
                              render_system_prompt, today_hint)
from app.core.registry import ToolRegistry
from app.core.skills import load_skills
from app.core.token_utils import estimate_tokens
from app.datasource.amap_web import AmapWebDataSource
from app.tools import register_all_tools
from app.tools.file import register_file_tools
from app.tools.knowledge import register_knowledge_tools
from app.tools.sub_agent import make_call_sub_agent

logger = logging.getLogger(__name__)

# 硬数据工具集合：这些工具返回的是「事实性」数据（车次/航班/天气），最容易被 LLM 编造或扭曲，
# 只有本轮调用了它们才触发 critic 回路（省成本，仅在硬数据场景做事实校验）。
HARD_DATA_TOOLS = {"train_ticket_query", "flight_query", "weather_query", "holiday_calendar"}


@dataclass
class _TurnState:
    """一轮对话的跨阶段累积状态：生成阶段写入，critic 与落库阶段读取。"""
    answer: str = ""
    tools: list = field(default_factory=list)
    hard_results: list = field(default_factory=list)
    loaded_skill_names: list = field(default_factory=list)
    rounds: list = field(default_factory=list)  # 每轮的 tool_calls（ReAct 轮次），供短期记忆按轮次落库


class AgentService:
    """多业务通用 service：装配会话历史 + 短期/长期记忆 + LLM + 数据源，驱动「通用 agent → 工具 → critic 校验」链路。

    单层架构 + 标准 skill 两段式：顶层 agent 直接调用各业务工具（不再委派子 agent）；
    system prompt 只注入「可用业务清单」（name+description），业务规则正文由 get_skill 工具按需加载。
    """

    def __init__(self):
        # 会话历史 + 短期记忆 + 长期记忆：三者共用 app.core.db 的进程级连接池。
        # store 只持有引用、不做启动期 IO（表结构由 deploy/schema.sql 建），
        # 因此 service 作为模块级单例在 import 时构造、连接池在 lifespan 里稍后建立，两者互不阻塞。
        self.conversation_store = ConversationStore()
        self.short_term_memory = ShortTermMemory()
        self.long_term_memory = LongTermMemory()
        # 知识库：全局共享的攻略语料，与长期记忆共用同一个 embedder 单例
        self.knowledge = KnowledgeStore()
        # 工具结果地址索引：超长结果写临时文件，read_file/grep 按需读；本轮结束即清，避免堆积
        self.tool_result_store = ToolResultStore(settings.result_dir)
        # LLM 客户端
        self.llm = get_llm()
        # 记忆管理器：编排短期记忆组装/压缩与长期记忆提炼，四个 store 与 AgentService 共享同一实例
        self.memory = MemoryManager(self.conversation_store, self.short_term_memory,
                                    self.long_term_memory, self.tool_result_store, self.llm)
        # 加载 skills 清单（name/description/body）：name+description 进 system prompt，body 由 get_skill 按需加载
        self.skills = load_skills()
        # 一次性装配扁平 ToolRegistry（旅行 + 系统 + 网络工具）并缓存到 self。
        # 关键：Train/Flight/HTTP 数据源在各 register_*_tools 里内联 new，其 __init__ 会创建
        # httpx 客户端（mcp_base）且从不 close；若每请求重建，高频 SSE 下连接持续泄漏。
        # 这里启动时 build 一次，chat_stream 直接复用，数据源只在启动时 new 一次。
        self.registry = self._build_registry()

    def _build_registry(self) -> ToolRegistry:
        """装配扁平 ToolRegistry：统一注册所有工具（旅行 + 系统 + 网络），只在启动时调用一次。

        同时收集所有「内联 new 的数据源」到 self._datasources，供 close() 统一关闭
        （httpx 客户端 / MCP session 都是进程级长连接，进程退出前应显式关闭）。
        """
        registry = ToolRegistry()
        amap_web_ds = AmapWebDataSource()  # 高德数据源只 new 一次，旅行/通用工具共享
        self._datasources = [amap_web_ds]
        # register_all_tools 内部会再 new UAPIS/Tavily/12306/机票数据源，返回它们供统一 close
        self._datasources.extend(register_all_tools(registry, amap_web_ds, [s["name"] for s in self.skills]))
        # read_file/grep 工具：地址索引的按需读出口（需要 tool_result_store，故在 service 层注册）
        register_file_tools(registry, self.tool_result_store)
        # search_knowledge：知识库语义检索（需要 knowledge store，同理由 service 层注册）
        register_knowledge_tools(registry, self.knowledge)
        # 注册 call_sub_agent（子 Agent 委派，需要 llm + skill 清单 + 工具结果地址索引，只能在 service 层注册）
        sub_agent_fn = make_call_sub_agent(self.llm, registry, [s["name"] for s in self.skills],
                                           settings.llm_context_budget,
                                           self.tool_result_store)
        registry.register("call_sub_agent", sub_agent_fn.description, sub_agent_fn.parameters, sub_agent_fn,
                          "委派子任务")
        return registry

    async def close(self):
        """优雅关闭所有数据源与 LLM 客户端（进程退出前由 lifespan 调用）。

        数据源持有从不显式关闭的 httpx 客户端 / MCP session，统一在这里关闭，
        避免热重载/测试时留下未关闭连接。LLM 客户端若无 close 方法（Fake/Fallback）则跳过。
        """
        for ds in self._datasources:
            ds_close = getattr(ds, "close", None)
            if ds_close is not None:
                await ds_close()
        llm_close = getattr(self.llm, "close", None)
        if llm_close is not None:
            await llm_close()

    async def delete_conversation(self, user_id, conv_id):
        """删除会话及其消息，并清理该会话的短期记忆与工具结果地址索引；归属校验失败返回 False。"""
        return await self.memory.delete_conversation(user_id, conv_id)

    async def chat_stream(self, user_id, conv_id, message):
        """SSE 流式对话主流程，依次产出事件：conversation_id → token/tool_call/tool_result → done。

        返回 AsyncIterator[dict]，每个 dict 的 "type" 字段标识事件类型。
        """
        # 设置请求级 user_id contextvar，供工具层（read_file/grep 按用户隔离读）与 run_tool 写文件使用
        current_user_id.set(user_id)
        logger.info("[service] 收到请求，user=%s，消息：%.60s", user_id, message)

        conv_id = await self._ensure_conversation(user_id, conv_id, message)
        yield {"type": "conversation_id", "conversation_id": conv_id}
        # 落库本轮用户消息
        await self.conversation_store.add_message(user_id, conv_id, "user", message)

        system, history, summary_hint, agent = await self._prepare_context(user_id, conv_id, message)

        # 事件队列：worker 在独立 task 里跑完整轮（生成→critic→落库→记忆→token 输出），
        # 主协程只负责从 queue 取事件转发 SSE。客户端刷新断开只会取消主协程（转发），
        # worker 不受影响，照常跑完并落库——保证「后端生成完，前端再刷新也能拉到」。
        queue: asyncio.Queue = asyncio.Queue()

        async def run_full_turn():
            """后台跑完完整一轮，把所有要下发的事件塞进 queue。

            放在独立 task 而非主协程里，是为了让落库不依赖「SSE 转发」这条可被
            客户端断开取消的路径：断开后 worker 继续执行到 add_message，答案照常入库。
            """
            state = _TurnState()
            try:
                # 工具调用阶段：run_stream 产出的事件转发进 queue（token 不立即下发，等 critic 后统一输出）
                async for sse in self._stream_agent_reply(agent, history, conv_id, message, state, summary_hint):
                    await queue.put(sse)
                # 工具调用结束，进入「整理答案」阶段：有硬数据先做事实校验，校验不过再修正
                await queue.put({"type": "status", "status": "checking" if state.hard_results else "generating"})
                if state.hard_results:
                    async for sse in self._critic_correct(state, system, message, user_id, summary_hint):
                        await queue.put(sse)

                answer = state.answer
                # 先落库完整答案（流式输出之前）：即使客户端中途刷新、SSE 流被中断，历史里也已存有本轮完整回复
                await self.conversation_store.add_message(
                    user_id, conv_id, "assistant",
                    json.dumps({"content": answer, "tools": state.tools}, ensure_ascii=False))
                # 补写本轮到短期记忆（按 ReAct 轮次），让「记忆」页立即显示最新一轮
                await self.memory.append_turn(user_id, conv_id, message, answer, state.rounds)
                # 流式输出最终答案：拆小块逐块上发（此时已过 critic 校验，保证只输出一段干净答案）
                for j in range(0, len(answer), 40):
                    await queue.put({"type": "token", "content": answer[j:j + 40]})
                # 清理本轮 spill 文件（保留 _history.txt）；异步提炼长期记忆（fire-and-forget，不影响主流程返回）
                await self.tool_result_store.clear_spill(conv_id, user_id)
                asyncio.create_task(self.memory.extract_facts(user_id, message))
                await queue.put({"type": "done"})
            finally:
                # 终止标记：成功或异常都放，保证主协程能退出；异常随后在 await worker 处重新抛出
                await queue.put(None)

        worker = asyncio.create_task(run_full_turn())
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield ev
        await worker  # 正常流程等 worker 收尾（保证落库完成后再返回）；断开时主协程被取消，worker 继续后台落库

    async def _ensure_conversation(self, user_id, conv_id, message):
        """无会话则新建（首次对话）并回写标题；客户端传入他人 conv_id 时当作首次对话重建，杜绝跨用户写入。"""
        if conv_id is None or not await self.conversation_store.belongs_to(user_id, conv_id):
            conv_id = await self.conversation_store.create_conversation(user_id)
            await self.conversation_store.update_title(user_id, conv_id, message[:20])
        return conv_id

    async def _prepare_context(self, user_id, conv_id, message):
        """准备阶段：召回长期记忆 + 组装短期历史 + 渲染 system prompt，返回 (system, history, summary_hint, agent)。"""
        # 长期记忆召回：按本轮问题语义检索最相关的 20 条（向量链路不可用时回退 importance 排序），
        # 再按 token 预算截断（占 long_term_hint_ratio），避免历史偏好无限挤占留给对话的预算；
        # 文案由 render_long_term_hint 集中拼装。
        facts = await self.long_term_memory.recall(user_id, query=message, top_n=20)
        hint_budget = int(settings.llm_context_budget * settings.long_term_hint_ratio)
        hint_parts = []
        hint_used = 0
        for f in facts:
            cost = estimate_tokens(f["fact"])
            if hint_used + cost > hint_budget:
                break
            hint_parts.append(f["fact"])
            hint_used += cost
        # 组装历史（running summary）：预算内保留最近回合原文，超预算的早期回合压缩成摘要注入 system
        history, summary = await self.memory.assemble_history(user_id, conv_id)
        # 可用业务清单（name + description）注入 system prompt；正文由 get_skill 工具按需加载
        skill_directory = "\n".join(f"- {s['name']}：{s['description']}" for s in self.skills)
        summary_hint = render_history_summary(summary, conv_id)
        system = render_system_prompt(
            skill_directory, load_prompt("grounding"),
            today=today_hint(),
            long_term_hint=render_long_term_hint(hint_parts),
        )
        agent = Agent(name="assistant", system_prompt=system, tools=self.registry.list_names(), llm=self.llm)
        return system, history, summary_hint, agent

    async def _stream_agent_reply(self, agent, history, conv_id, message, state, summary_hint=""):
        """跑 ReAct 生成，把内部事件转成 SSE 转发，同时累积 tools/hard_results/loaded_skill_names 到 state。

        结束时把最终答案写入 state.answer；summary_hint 作为历史摘要传给 run_stream（独立 system 消息）。
        """
        queue: asyncio.Queue = asyncio.Queue()
        pending_calls: list[dict] = []  # 待配对的 tool_call（工具并行执行时按 tool_call_id 匹配回 result）

        async def emit(event):
            # 把 run_stream 的内部事件原样塞进队列，供下方循环实时转 SSE
            await queue.put(event)

        async def generate():
            # 真正执行一轮 agent 生成；结束时放 None 作为队列终止标记
            try:
                return await agent.run_stream(
                    message, history, self.registry, on_event=emit,
                    conversation_id=conv_id,
                    context_budget=settings.llm_context_budget,
                    tool_result_store=self.tool_result_store,
                    summary=summary_hint,
                )
            finally:
                await queue.put(None)

        async def consume():
            # 消费队列：把 token/tool_call/tool_result 转成 SSE，同时累积状态供 critic 与持久化使用
            while True:
                ev = await queue.get()
                if ev is None:
                    return
                etype = ev["type"]
                if etype == "token":
                    pass  # 答案不立即输出：等 critic 校验通过后统一流式输出，避免「第一段编造答案」先流出
                elif etype == "status":
                    yield {"type": "status", "status": ev["status"]}
                elif etype == "tool_call":
                    # 入队待配对（带 tool_call_id），label 一并入队随 tools 持久化供前端还原中文名
                    label = ev.get("label", ev["tool"])
                    pending_calls.append({"tool": ev["tool"], "args": ev["args"], "id": ev["id"], "label": label})
                    # 记录 get_skill 加载的业务，供 critic 修正时注入规则正文（保住输出格式约束）
                    if ev["tool"] == "get_skill":
                        args = ev.get("args") or {}
                        if isinstance(args, dict) and args.get("name"):
                            state.loaded_skill_names.append(args["name"])
                    # id/label 一并下发给前端：同名工具并发时前端也能按 id 精确配对 summary，不串
                    yield {"type": "tool_call", "tool": ev["tool"], "args": ev["args"], "id": ev["id"], "label": label}
                elif etype == "tool_result":
                    name = ev["tool"]
                    # 按 tool_call_id 精确配对：并行执行时工具按「完成先后」返回，tool_result 顺序与 tool_call
                    # 顺序不一致，按名或按队列顺序都会在同名工具并发时错配，只有 tool_call_id 是唯一可靠键。
                    entry = next((p for p in pending_calls if p["id"] == ev["id"]), None)
                    if entry is not None:
                        pending_calls.remove(entry)
                        entry["status"] = True
                        # result = 模型工作记忆里的截断版结果（模型所见语料），供 critic 校验、修正重写与前端展示复用
                        entry["result"] = ev.get("result", "")
                        # id 转成 tool_call_id 保留：供 assemble_history 跨轮还原 role=tool 消息时配对
                        entry["tool_call_id"] = entry.pop("id")
                        state.tools.append(entry)
                    if name in HARD_DATA_TOOLS:
                        # 硬数据工具记模型所见语料（ev["result"]），保证 critic 与模型生成答案时语料一致
                        state.hard_results.append(ev.get("result", ""))
                    yield {"type": "tool_result", "tool": name, "status": ev.get("status", True),
                           "result": ev.get("result", ""), "id": ev["id"], "label": ev.get("label", name)}

        gen_task = asyncio.create_task(generate())
        async for sse in consume():
            yield sse
        state.answer, state.rounds = await gen_task

    async def _critic_correct(self, state, system, message, user_id, summary_hint=""):
        """critic 事实校验：不通过时复用已查工具结果让 LLM 重写答案（不重新调工具）；可能更新 state.answer。

        summary_hint 作为独立 system 消息随修正 prompt 一起注入，保持与主生成一致的历史上下文。
        """
        # read_file_fn / grep_fn：绑定当前 user_id 的完整结果读取，供 critic 多轮定向读文件
        async def read_file_fn(path, offset, limit):
            return await self.tool_result_store.read(path, offset, limit, user_id=user_id)

        async def grep_fn(pattern, path):
            return await self.tool_result_store.search(path, pattern, user_id=user_id)

        verdict = await run_critic(self.llm, json.dumps(state.hard_results, ensure_ascii=False), state.answer,
                                   read_file_fn=read_file_fn, grep_fn=grep_fn)
        if verdict.get("ok", True):
            return
        # ok=false：第一轮工具结果本身是准的，问题只在模型生成答案时编造了价格。
        # 修正时复用第一轮的工具结果，直接让模型重新生成答案——不重新调工具、不重新走 ReAct。
        issues = verdict.get("issues", [])
        logger.info("[service] critic 判定不通过（%d 条问题），进入修正答案", len(issues))
        yield {"type": "status", "status": "correcting"}
        # 修正 prompt 只喂 {tool,args,result} 事实依据，result 取模型所见语料（截断版）而非展示用 summary
        tool_results_text = json.dumps(
            [{"tool": t["tool"], "args": t["args"], "result": t.get("result", "")}
             for t in state.tools], ensure_ascii=False)
        # 注入本轮 get_skill 加载过的业务规则正文，保住「输出格式」等约束（修正走 complete 不重新调 get_skill）
        skill_guide = "\n\n".join(
            f"【{s['name']} 业务规则】\n{s['body']}"
            for s in self.skills if s["name"] in state.loaded_skill_names
        )
        correction_prompt = (
                f"原始问题：{message}\n\n"
                f"以下是本轮已经查询到的工具结果（事实依据，请严格基于这些，不要编造）：\n{tool_results_text}\n\n"
                + (
                    f"以下是本业务的规划规则与输出格式，修正后的答案必须同样遵守：\n{skill_guide}\n\n" if skill_guide else "")
                + f"请基于以上工具结果直接输出修正后的最终答案（中文 Markdown）。"
                  f"用户明确询问但工具没查到的关键信息，如实说明「该信息未查到」，不要编造；用户没问的字段不要罗列缺失。\n"
                  f"修正要点：\n{json.dumps(issues, ensure_ascii=False)}"
        )
        t0 = time.perf_counter()
        msgs = [{"role": "system", "content": system}]
        if summary_hint:
            msgs.append({"role": "system", "content": summary_hint})
        msgs.append({"role": "user", "content": correction_prompt})
        state.answer = await self.llm.complete(msgs)
        logger.info("[service] 修正答案完成，耗时 %.1fs，%d 字", time.perf_counter() - t0, len(state.answer))


# 进程级单例：路由层共用同一个 service，避免重复装配/重复创建 LLM 与数据源客户端
service = AgentService()
