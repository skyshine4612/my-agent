# app/services/agent_service.py
# 多业务通用 service：把会话历史、短期记忆、长期记忆、LLM、数据源、业务工具
# 串成一个完整的 AgentService，对外提供 SSE 流式对话 chat_stream；记忆编排委托给 core/memory/manager.py。
import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.config import settings
from app.core.agent import HARD_DATA_TOOLS, Agent, CriticConfig
from app.core.context import current_conversation_id, current_user_id
from app.core.files import FileStore
from app.core.knowledge import KnowledgeStore
from app.core.llm import close_chat_model, get_chat_model
from app.core.memory import ConversationStore, LongTermMemory, ShortTermMemory, ToolResultStore
from app.core.memory.manager import MemoryManager
from app.core.memory.metrics import log_snapshot
from app.core.prompts import (load_prompt, render_file_directory, render_history_summary,
                              render_long_term_hint, render_system_prompt, today_hint)
from app.core.registry import ToolRegistry
from app.core.sandbox import SandboxClient
from app.core.skill_scripts import scan_scripts
from app.core.skills import load_skills
from app.core.token_utils import estimate_tokens
from app.datasource.amap_web import AmapWebDataSource
from app.tools import register_all_tools
from app.tools.file import make_grep, make_read_file, register_file_tools
from app.tools.knowledge import register_knowledge_tools
from app.tools.script import register_script_tools
from app.tools.sub_agent import make_call_sub_agent

logger = logging.getLogger(__name__)


@dataclass
class _TurnState:
    """一轮对话的跨阶段累积状态：生成阶段写入，落库阶段读取。"""
    answer: str = ""
    tools: list = field(default_factory=list)
    hard_results: list = field(default_factory=list)
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
        # 会话文件：上传件与沙箱产物，与 result_dir（spill）分两棵树
        self.file_store = FileStore(settings.file_dir)
        # 沙箱客户端：长连接复用，随 registry 一起装配一次
        self.sandbox = SandboxClient(settings.sandbox_url, settings.sandbox_timeout)
        # 脚本白名单：启动时扫描一次，run_skill_script 的 enum 与校验都用这份索引
        self.skill_scripts = scan_scripts()
        # langchain 模型：全项目唯一的 LLM 通路口（主循环 / critic / 记忆蒸馏 / 修正重写共用）
        self.chat_model = get_chat_model()
        # critic 子图的工具：与主循环共用同一套 read_file/grep 实现，启动时装配一次
        self.critic_tools = [make_read_file(self.tool_result_store), make_grep(self.tool_result_store)]
        # 记忆管理器：编排短期记忆组装/压缩与长期记忆提炼，四个 store 与 AgentService 共享同一实例
        self.memory = MemoryManager(self.conversation_store, self.short_term_memory,
                                    self.long_term_memory, self.tool_result_store, self.chat_model)
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
        # run_skill_script：在沙箱里跑仓库里已评审的脚本（需要沙箱客户端 + 文件存储 + 脚本索引）
        register_script_tools(registry, self.sandbox, self.file_store, self.skill_scripts)
        # 注册 call_sub_agent（子 Agent 委派，需要模型 + skill 清单 + 工具结果地址索引，只能在 service 层注册）
        sub_agent_tool = make_call_sub_agent(self.chat_model, registry, [s["name"] for s in self.skills],
                                             settings.llm_context_budget,
                                             self.tool_result_store)
        registry.register_tool(sub_agent_tool, "委派子任务")
        return registry

    async def close(self):
        """优雅关闭所有数据源与 LLM 连接池（进程退出前由 lifespan 调用）。

        数据源持有从不显式关闭的 httpx 客户端 / MCP session，统一在这里关闭，
        避免热重载/测试时留下未关闭连接。
        """
        for ds in self._datasources:
            ds_close = getattr(ds, "close", None)
            if ds_close is not None:
                await ds_close()
        await self.sandbox.close()
        await close_chat_model()

    async def delete_conversation(self, user_id, conv_id):
        """删除会话及其消息，并清理该会话的短期记忆、工具结果地址索引与会话文件；归属校验失败返回 False。

        result_dir 与 file_dir 是分开的两棵树（本轮清 vs 会话清），所以要分别清。
        """
        ok = await self.memory.delete_conversation(user_id, conv_id)
        if ok:
            await self.file_store.delete_conversation(user_id, conv_id)
        return ok

    async def chat_stream(self, user_id, conv_id, message):
        """SSE 流式对话主流程，依次产出事件：conversation_id → token/tool_call/tool_result → done。

        返回 AsyncIterator[dict]，每个 dict 的 "type" 字段标识事件类型。
        """
        # 设置请求级 user_id contextvar，供工具层（read_file/grep 按用户隔离读、
        # _ToolExecMiddleware 写文件）使用
        current_user_id.set(user_id)
        logger.info("[service] 收到请求，user=%s，消息：%.60s", user_id, message)

        conv_id = await self._ensure_conversation(user_id, conv_id, message)
        # 工具层要按会话定位文件目录，与 user_id 一起注入请求级上下文
        current_conversation_id.set(conv_id)
        yield {"type": "conversation_id", "conversation_id": conv_id}
        # 落库本轮用户消息
        await self.conversation_store.add_message(user_id, conv_id, "user", message)

        history, summary_hint, agent = await self._prepare_context(user_id, conv_id, message)

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
                # 工具调用阶段：run_stream 产出的事件转发进 queue（token 不立即下发，等 critic 后统一输出）。
                # critic 校验与修正改写挂在 ReAct 收口钩子上，其 checking / correcting 状态随事件流按序到达
                async for sse in self._stream_agent_reply(agent, history, conv_id, message, state, summary_hint):
                    await queue.put(sse)
                # 工具调用结束，进入「整理答案」阶段：硬数据轮次的 checking 已由收口钩子发出，这里只补非校验轮次的标记
                if not state.hard_results:
                    await queue.put({"type": "status", "status": "generating"})

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
                # 观测出口：每轮收尾打一条指标快照日志，供线上观察
                log_snapshot()
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
        """准备阶段：召回长期记忆 + 组装短期历史 + 渲染 system prompt，返回 (history, summary_hint, agent)。"""
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
        # 本会话可用文件（上传件 + 脚本产物）：模型任何时候都看得见有哪些文件、file_id 是什么
        file_directory = render_file_directory(
            await self.file_store.list_files(user_id, conv_id, limit=10))
        system = render_system_prompt(
            skill_directory, load_prompt("grounding"),
            today=today_hint(),
            long_term_hint=render_long_term_hint(hint_parts),
            file_directory=file_directory,
        )
        agent = Agent(name="assistant", system_prompt=system, tools=self.registry.list_names(),
                      model=self.chat_model)
        return history, summary_hint, agent

    async def _stream_agent_reply(self, agent, history, conv_id, message, state, summary_hint=""):
        """跑本轮生成，把内部事件转成 SSE 转发，同时累积 tools/hard_results 到 state。

        结束时把最终答案（critic 修正过则为修正版）写入 state.answer；
        summary_hint 作为历史摘要传给 run_stream（独立 system 消息）。
        critic 回路随本轮装配一起交给 run_stream（校验语料与修正所需的工具结果都从图 state 取）。
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
                    # critic 回路随本轮装配交给 run_stream：校验与修正所需的本轮工具结果从图 state 取
                    critic=CriticConfig(read_file_tool=self.critic_tools[0],
                                        grep_tool=self.critic_tools[1],
                                        skills=self.skills),
                )
            finally:
                await queue.put(None)

        async def consume():
            # 消费队列：把 token/tool_call/tool_result 转成 SSE，同时累积状态供持久化使用
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
                        # 产物清单随消息落库：刷新后从历史还原工具气泡时，下载入口还在
                        if ev.get("artifacts"):
                            entry["artifacts"] = ev["artifacts"]
                        # id 转成 tool_call_id 保留：供 assemble_history 跨轮还原 role=tool 消息时配对
                        entry["tool_call_id"] = entry.pop("id")
                        state.tools.append(entry)
                    if name in HARD_DATA_TOOLS:
                        # 硬数据工具记模型所见语料（ev["result"]），保证 critic 与模型生成答案时语料一致
                        state.hard_results.append(ev.get("result", ""))
                    sse = {"type": "tool_result", "tool": name, "status": ev.get("status", True),
                           "result": ev.get("result", ""), "id": ev["id"], "label": ev.get("label", name)}
                    if ev.get("artifacts"):
                        sse["artifacts"] = ev["artifacts"]
                    yield sse

        gen_task = asyncio.create_task(generate())
        async for sse in consume():
            yield sse
        state.answer, state.rounds = await gen_task


# 进程级单例：路由层共用同一个 service，避免重复装配/重复创建 LLM 与数据源客户端
service = AgentService()
