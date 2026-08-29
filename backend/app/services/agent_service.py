# app/services/agent_service.py
# 多业务通用 service：把记忆（会话 + 长期）、LLM、数据源、业务工具与 call_sub_agent
# 串成一个完整的 AgentService，对外提供 SSE 流式对话 chat_stream 与长期记忆异步提炼。
# 替代旧的 trip_service（旅行专用「规划 → 澄清/执行 → 三套方案」链路）。
import asyncio
import difflib
import json
import logging

from app.config import settings
from app.core.agent import Agent
from app.core.critic import run_critic
from app.core.llm import get_llm
from app.core.memory import ConversationStore, LongTermMemory
from app.core.prompts import load_prompt, render_system_prompt
from app.core.registry import ToolRegistry
from app.core.sub_agent import make_call_sub_agent
from app.businesses import BUSINESSES
from app.datasource.amap_mcp import AmapMcpDataSource

logger = logging.getLogger(__name__)

# 硬数据工具集合：这些工具返回的是「事实性」数据（车次/航班/天气），最容易被 LLM 编造或扭曲，
# 只有本轮调用了它们才触发 critic 回路（省成本，仅在硬数据场景做事实校验）。
HARD_DATA_TOOLS = {"train_ticket_query", "flight_query", "weather_query"}


def _similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """简单字符串相似度：用 difflib.SequenceMatcher 判断两条 fact 是否语义相近（去重用）。"""
    return difflib.SequenceMatcher(None, a, b).ratio() > threshold


class AgentService:
    """多业务通用 service：装配会话/长期记忆 + LLM + 数据源，驱动「通用 agent → 工具/委派 → critic 校验」链路。"""

    def __init__(self):
        # 会话 + 长期记忆，共享同一 SQLite（路径可配置，容器内挂载 ./data）
        self.conv = ConversationStore(settings.db_path)
        self.ltm = LongTermMemory(settings.db_path)
        # LLM 客户端 + 真实高德数据源（火车/机票数据源由旅行业务在注册工具时内联创建）
        self.llm = get_llm()
        self.ds = AmapMcpDataSource()

    def _build_registry(self) -> ToolRegistry:
        """装配扁平 ToolRegistry：各业务工具 + call_sub_agent（供顶层 agent 一次性绑定）。"""
        registry = ToolRegistry()
        businesses_map = {b.name: b for b in BUSINESSES}
        # 遍历已注册业务，把各业务工具注入同一个扁平注册表
        for b in BUSINESSES:
            b.register_tools(registry, self.ds)
        # call_sub_agent：把复杂任务委派给对应业务的子 agent，description/parameters 取自工具对象
        call_sub_agent = make_call_sub_agent(businesses_map, self.llm, registry)
        registry.register("call_sub_agent", call_sub_agent.description, call_sub_agent.parameters, call_sub_agent)
        return registry

    async def chat_stream(self, user_id, conv_id, message):
        """SSE 流式对话主流程，依次产出事件：conversation_id → token/tool_call/tool_result → done。

        返回 AsyncIterator[dict]，每个 dict 的 "type" 字段标识事件类型。
        """
        logger.info("[service] 收到请求，user=%s，消息：%.60s", user_id, message)
        # 1. 无会话则新建（首次对话），并先回传 conversation_id 让前端记住会话。
        #    写路径归属校验：客户端传入他人 conv_id 时当作首次对话重建，杜绝跨用户写入。
        if conv_id is None or not await self.conv.belongs_to(user_id, conv_id):
            conv_id = await self.conv.create_conversation(user_id)
            await self.conv.update_title(conv_id, message[:20])
        yield {"type": "conversation_id", "conversation_id": conv_id}
        # 2. 落库本轮用户消息
        await self.conv.add_message(conv_id, "user", message)
        # 3. LTM 召回：按 importance 降序取前 20 条，拼成「用户历史偏好」注入 system 上下文
        facts = await self.ltm.recall(user_id, top_n=20)
        fact_hint = "用户历史偏好：" + "；".join(f["fact"] for f in facts) if facts else ""
        # 4. 组装历史：assistant 消息若是落库的 JSON {"content","tools"} 结构则取 content（markdown），
        #    user 消息原文；压缩交给 run_stream 内的 WorkingMemory，本层不手动压缩。
        raw_history = await self.conv.get_history(user_id, conv_id)
        history = []
        for h in raw_history:
            if h["role"] == "user":
                history.append({"role": "user", "content": h["content"]})
            else:
                content = h["content"]
                try:
                    data = json.loads(content)
                    if isinstance(data, dict) and isinstance(data.get("content"), str):
                        content = data["content"]
                except (json.JSONDecodeError, TypeError):
                    pass   # 非 JSON 的 assistant 消息原样保留
                history.append({"role": "assistant", "content": content})
        # 末尾一条是本轮刚落库的用户消息，run_stream 会再追加一次，这里剔除避免重复
        if history and history[-1]["role"] == "user":
            history = history[:-1]
        # 5. 装配工具 + 顶层 agent
        registry = self._build_registry()
        tool_grounding = load_prompt("grounding")
        business_directory = "\n".join(f"- {b.name}：{b.description}" for b in BUSINESSES)
        system = render_system_prompt(business_directory, tool_grounding)
        if fact_hint:
            system += "\n" + fact_hint
        agent = Agent(name="assistant", system_prompt=system, tools=registry.list_names(), llm=self.llm)

        # 7. 跑 run_stream：on_event 把事件塞队列，主循环消费队列转 SSE。
        #    本地累积 tools（每对 tool_call/tool_result）与 hard_results（硬数据工具完整结果）供 critic。
        queue: asyncio.Queue = asyncio.Queue()
        tools: list[dict] = []
        hard_results: list[str] = []
        pending_calls: list[dict] = []   # 待配对的 tool_call（工具并行执行时按名匹配回 result）

        async def emit(event):
            # 把 run_stream 的内部事件原样塞进队列，供下方循环实时转 SSE
            await queue.put(event)

        async def generate(user_message):
            # 真正执行一轮 agent 生成；结束时放 None 作为队列终止标记
            try:
                return await agent.run_stream(user_message, history, registry, on_event=emit)
            finally:
                await queue.put(None)

        async def consume():
            # 消费队列：把 token/tool_call/tool_result 转成 SSE，忽略 agent_start/agent_think/agent_done；
            # 同时累积 tools 与 hard_results，供持久化与 critic 使用。
            while True:
                ev = await queue.get()
                if ev is None:
                    return
                etype = ev["type"]
                if etype == "token":
                    yield {"type": "token", "content": ev["content"]}
                elif etype == "tool_call":
                    pending_calls.append({"tool": ev["tool"], "args": ev["args"]})
                    yield {"type": "tool_call", "tool": ev["tool"], "args": ev["args"]}
                elif etype == "tool_result":
                    name = ev["tool"]
                    # 按名匹配最近一次未配对的 tool_call（并行执行时 result 顺序可能不同于 call 顺序）
                    entry = next((p for p in pending_calls if p["tool"] == name), None)
                    if entry is not None:
                        pending_calls.remove(entry)
                        entry["summary"] = ev["summary"]
                        tools.append(entry)
                    if name in HARD_DATA_TOOLS:
                        # 硬数据工具记完整结果（ev["full"]），供 critic 校验回答
                        hard_results.append(ev.get("full", ev["summary"]))
                    yield {"type": "tool_result", "tool": name, "summary": ev["summary"]}
                # 其余事件类型（agent_start / agent_think / agent_done）不转发给前端

        # 第一轮生成
        gen_task = asyncio.create_task(generate(message))
        async for sse in consume():
            yield sse
        answer = await gen_task

        # 8. critic 回路：仅当本轮调用了硬数据工具才校验（否则无事实依据可核对）
        if hard_results:
            verdict = await run_critic(self.llm, json.dumps(hard_results, ensure_ascii=False), answer)
            if not verdict.get("ok", True):
                # ok=false：把 issues 作为修正指令再生成一轮（不递归），用修正后的新回答
                issues = verdict.get("issues", [])
                correction = "请修正以下问题后重新输出：\n" + json.dumps(issues, ensure_ascii=False)
                gen_task = asyncio.create_task(generate(correction))
                async for sse in consume():
                    yield sse
                answer = await gen_task

        # 9. 持久化：markdown 内容 + 本轮工具调用记录，作为结构化 JSON 落库 assistant 消息
        await self.conv.add_message(conv_id, "assistant",
                                    json.dumps({"content": answer, "tools": tools}, ensure_ascii=False))
        # 10. 异步提炼长期记忆（fire-and-forget，不影响主流程返回）
        asyncio.create_task(self._extract_facts(user_id, conv_id))
        yield {"type": "done"}

    async def _extract_facts(self, user_id, conv_id):
        """异步提炼长期记忆：只从用户发言提取跨会话可复用偏好，并做语义去重。

        关键：只提取 user 角色的消息，不把 agent 返回的方案内容误当成用户偏好。
        去重：新 fact 与已有 fact 语义相近时，提升已有 fact 的 importance 而非新增重复条目。
        """
        try:
            msgs = await self.conv.get_history(user_id, conv_id)
            user_msgs = [m for m in msgs if m["role"] == "user"]
            if not user_msgs:
                return
            resp = await self.llm.complete([
                {"role": "system", "content": "从下面的用户发言里，只提取【稳定的、跨会话可复用的】偏好或事实（如常住城市、长期的兴趣偏好、习惯性要求，例：'偏好经济型酒店'、'喜欢自然风光'）。不要提取一次性的行程请求参数（如'去成都玩2天预算2000'这种某次旅行的具体目的地/天数/预算），也不要提取助手推荐的内容。没有稳定偏好就输出空列表。每条打 importance(0~1)。输出 JSON:{\"facts\":[{\"fact\":\"...\",\"importance\":0.8}]}"},
                {"role": "user", "content": json.dumps(user_msgs, ensure_ascii=False)}])
            data = json.loads(resp[resp.index("{"):resp.rindex("}") + 1])
            new_facts = data.get("facts", [])
            if not new_facts:
                return
            existing = await self.ltm.get_all(user_id)
            to_add = []
            for nf in new_facts:
                fact_text = nf.get("fact", "")
                importance = nf.get("importance", 0.5)
                # 语义去重：与已有 fact 相似则更新其 importance（取较大值），否则标记为新增
                matched = next((ex for ex in existing if _similar(fact_text, ex["fact"])), None)
                if matched:
                    await self.ltm.update_importance(user_id, matched["fact"],
                                                      max(matched["importance"], importance))
                else:
                    to_add.append(nf)
            if to_add:
                await self.ltm.add_facts(user_id, to_add)
        except Exception:
            # 提炼失败不影响主对话流程，直接吞掉异常
            pass


# 进程级单例：路由层共用同一个 service，避免重复装配/重复创建 LLM 与数据源客户端
service = AgentService()
