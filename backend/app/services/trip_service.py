# app/services/trip_service.py
# 业务编排 service：把前面各核心模块（三层记忆、LLM、Planner、Orchestrator、数据源、Domain）
# 串成一个完整可用的 AgentService，对外提供 SSE 流式对话 plan_stream 与长期记忆异步提炼。
import asyncio
import json
import logging

from app.config import settings
from app.core.memory import ConversationStore, TaskMemory, LongTermMemory
from app.core.llm import get_llm
from app.core.planner import Planner
from app.core.orchestrator import Orchestrator, SubTask
from app.core.registry import AgentRegistry
from app.datasource.amap_mcp import AmapMcpDataSource
from app.domains.travel.agents import TravelDomain

logger = logging.getLogger(__name__)


class AgentService:
    """业务编排 service：装配三层记忆 + LLM + 数据源 + Domain + Planner + Orchestrator，
    并驱动「规划 → 澄清/执行 → 整合 → 长期记忆提炼」的完整对话流程。"""

    def __init__(self):
        # 三层记忆：会话、任务、长期记忆，共享同一 SQLite（路径可配置，容器内挂载 ./data）
        self.conv = ConversationStore(settings.db_path)
        self.tasks = TaskMemory(settings.db_path)
        self.ltm = LongTermMemory(settings.db_path)
        # LLM 客户端 + 真实高德数据源
        self.llm = get_llm()
        self.ds = AmapMcpDataSource()
        # 旅行域装配：得到工具注册表与 Agent 注册表
        self.domain = TravelDomain()
        self.tools, self.agents = self.domain.build(self.ds)
        # 关键：把 llm 注入到所有子 Agent（否则 agent.run 时 self.llm 是 None 会崩）
        for name in self.agents.list_names():
            self.agents.get(name).llm = self.llm
        # 规划器（判断信息完整性）与编排器（按 DAG 调度子 Agent）
        # 注意：Planner 只拆「搜索类」子 Agent，不含「规划Agent」——规划 Agent 由 service
        # 在整合阶段单独调用（否则会被当成子任务重复执行一次，导致结果错乱）
        search_agents = AgentRegistry()
        for name in self.agents.list_names():
            if name != "规划Agent":
                search_agents.register(self.agents.get(name))
        self.planner = Planner(self.llm, search_agents, self.domain.required_inputs)
        self.orchestrator = Orchestrator(self.agents, self.tasks)

    async def plan_stream(self, user_id, conv_id, message):
        """SSE 流式对话主流程，依次产出事件：
        conversation_id → clarification（信息不全时）或 plan_start → 每个子任务 task_start
        → task_done → final_result。

        返回 AsyncIterator[dict]，每个 dict 的 "type" 字段标识事件类型。
        """
        # 1. 无会话则新建（首次对话），并先回传 conversation_id 让前端记住会话。
        #    写路径归属校验：读路径 get_history 会校验归属，但写路径此前缺失——客户端传入
        #    他人 conv_id 时会直接 add_message 注入。这里补上 belongs_to 校验，无效会话一律
        #    当作首次对话重新建会话，从根上杜绝跨用户写入。
        # 日志：记录请求进入（用户 + 消息摘要），这是整条编排链路的起点
        logger.info("[编排] 收到请求，user=%s，消息：%.60s", user_id, message)
        if conv_id is None or not await self.conv.belongs_to(user_id, conv_id):
            conv_id = await self.conv.create_conversation(user_id)
            # 用第一条消息前 20 字作为会话标题，避免一直叫「新会话」
            await self.conv.update_title(conv_id, message[:20])
        yield {"type": "conversation_id", "conversation_id": conv_id}
        # 2. 落库本轮用户消息
        await self.conv.add_message(conv_id, "user", message)
        # 3. 长期记忆召回：把该用户历史偏好全量拼成提示，注入 system 上下文
        facts = await self.ltm.get_all(user_id)
        fact_hint = "用户历史偏好：" + "；".join(f["fact"] for f in facts) if facts else ""
        history = await self.conv.get_history(user_id, conv_id)

        # 4. Planner 判断信息完整性：把长期记忆提示 + 历史对话 + 当前请求一起送入。
        #    此前只传 fact_hint + 最新一条消息，多轮澄清场景（用户分多次补充"成都"→"4天"→"3000"）
        #    Planner 永远只能看到最后一轮，无法累积上下文。这里拼上历史对话，让 Planner 看到
        #    用户之前补充过的信息，多轮澄清在部分补齐时也能正确判断信息是否齐全。
        history_text, prev_items = await self._summarize_history(history)
        plan = await self.planner.plan(fact_hint + "\n历史对话：\n" + history_text + "\n当前请求：" + message)
        if plan.get("clarify"):
            # 信息不全：返回澄清问题，不执行，等用户补充关键信息
            logger.info("[编排] 信息不全，向用户澄清：%s", plan["questions"])
            yield {"type": "clarification", "questions": plan["questions"]}
            # 存成结构化 JSON，前端刷新后能还原成可点击的澄清气泡（而非纯文本）
            await self.conv.add_message(conv_id, "assistant",
                                        json.dumps({"clarify": True, "questions": plan["questions"]}, ensure_ascii=False))
            return

        # 5. Planner 返回的是 dict 列表，Orchestrator 需要 SubTask 对象，这里做一次转换
        subtasks = [SubTask(id=t["id"], agent=t["agent"],
                            params=t.get("params", {}), depends_on=t.get("depends_on", []))
                    for t in plan["tasks"]]
        logger.info("[编排] Planner 拆解出 %d 个子任务：%s", len(subtasks), [t.agent for t in subtasks])
        yield {"type": "plan_start", "tasks": [t.__dict__ for t in subtasks]}
        # 6. 先逐个广播 task_start（含子任务 id 与负责 Agent）
        for t in subtasks:
            yield {"type": "task_start", "task_id": t.id, "agent": t.agent}
        # 7. 先落库真实 task_id，再用队列并发执行：子 Agent 的思考过程（agent_* / tool_*）
        #    与子任务完成（task_done）都边执行边经队列实时推给前端，不再等全部跑完才一次吐出。
        queue: asyncio.Queue = asyncio.Queue()

        async def on_step(event):
            # 回调里把事件塞进队列，供下方循环实时 yield
            await queue.put(event)

        async def run_execute():
            # 后台真正执行编排；结束时放入 None 作为队列终止标记
            try:
                return await self.orchestrator.execute(subtasks, history, self.tools, task_id, on_step=on_step)
            finally:
                await queue.put(None)

        task_id = await self.tasks.create_task(conv_id, plan)
        execute_task = asyncio.create_task(run_execute())

        # 边执行边消费队列：把思考过程 / task_done 实时转成 SSE 事件推给前端
        while True:
            ev = await queue.get()
            if ev is None:
                break
            yield {"type": ev["type"], **{k: v for k, v in ev.items() if k != "type"}}

        # 等编排真正结束，拿到各子任务结果（task_done 已在上方实时推过）
        results = await execute_task

        # 8.5 搜索子任务都完成后，进入整合阶段（生成 3 套方案，较慢）；
        #     推送「生成中」事件让前端显示真实进度，避免「全部完成但结果还没来」的困惑
        yield {"type": "generating", "message": "正在生成 3 套方案…"}

        # 9. 并行生成 3 套不同侧重方案（每套独立 LLM 调用，并行执行大幅缩短等待）
        from app.domains.travel.prompts import PLANNER_AGENT_PROMPT
        # 用上一步统一摘要得到的「之前推荐过的关键项」，作为「换一批」时避免重复的提示
        avoid_hint = ""
        if prev_items:
            avoid_hint = f"\n注意：之前已推荐过这些，本次请避开、尽量推荐不同的：{'、'.join(prev_items)}"
        # 注入真实日期：避免 LLM 编造年份；同时尊重用户指定的出发日期（如「明天」）
        from datetime import date, timedelta
        today = date.today()
        tomorrow = (today + timedelta(days=1)).isoformat()
        date_hint = (f"\n注意：今天是 {today.isoformat()}。用户若说了出发日期（如「明天」即 {tomorrow}、或具体几号），"
                     f"行程必须从那个日期开始，不要默认今天。若用户没说出日期，则从 {tomorrow} 开始。每天 +1 天，严禁编造其他年份。")
        ctx = fact_hint + date_hint + avoid_hint + "\n" + "\n".join(f"{k}: {v}" for k, v in results.items())
        FOCI = [
            ("方案A：经典必去", "最经典的地标景点"),
            ("方案B：自然风光", "偏户外/风景/休闲慢游"),
            ("方案C：美食人文", "深度美食探店+文化体验"),
        ]

        async def gen_one(name, focus):
            # 每套方案用不同的 system prompt（侧重不同），直接调 LLM 生成单套方案
            try:
                system = PLANNER_AGENT_PROMPT.format(name=name, focus=focus)
                resp = await self.llm.chat(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": ctx}],
                    response_format={"type": "json_object"})
                return self._parse_json(resp["content"])
            except Exception as e:
                # 单套生成失败（如百炼 QPS 并发限制）不阻断整体，返回 None 跳过
                logger.warning("[编排] 方案 %s 生成失败：%s", name, e)
                return None

        texts = await asyncio.gather(*(gen_one(name, focus) for name, focus in FOCI))
        plans = []
        for (name, focus), text in zip(FOCI, texts):
            if text is None:
                continue
            if isinstance(text, dict) and isinstance(text.get("plan"), dict):
                plans.append({"name": name, "focus": focus, "plan": text["plan"]})
            elif isinstance(text, dict) and "city" in text:
                plans.append({"name": name, "focus": focus, "plan": text})
        final = {"plans": plans}

        # 后处理：对每套方案覆盖 LLM 编造的 location/photo 为真实坐标与图片
        for p in final.get("plans", []):
            await self._enrich_plan(p["plan"])
        await self.conv.add_message(conv_id, "assistant", json.dumps(final, ensure_ascii=False))
        # 10. 异步提炼长期记忆（fire-and-forget，不影响主流程返回）
        asyncio.create_task(self._extract_facts(user_id, conv_id))
        logger.info("[编排] 生成行程完成，方案数=%d", len(final.get("plans", [])))
        yield {"type": "final_result", "data": final}

    async def _extract_facts(self, user_id, conv_id):
        """异步提炼长期记忆：只从用户自己的发言里提取偏好/事实，静默失败不影响主流程。

        关键：只提取 user 角色的消息，不把 agent 返回的方案内容（景点/美食等推荐）
        误当成用户偏好——用户没说喜欢哪个方案，那些只是推荐结果。
        """
        try:
            msgs = await self.conv.get_history(user_id, conv_id)
            # 只保留用户说过的话，排除助手返回的方案 JSON
            user_msgs = [m for m in msgs if m["role"] == "user"]
            if not user_msgs:
                return
            resp = await self.llm.complete([
                {"role": "system", "content": "从下面的用户发言里，只提取【稳定的、跨会话可复用的】偏好或事实（如常住城市、长期的兴趣偏好、习惯性要求，例：'偏好经济型酒店'、'喜欢自然风光'）。不要提取一次性的行程请求参数（如'去成都玩2天预算2000'这种某次旅行的具体目的地/天数/预算），也不要提取助手推荐的内容。没有稳定偏好就输出空列表。每条打 importance(0~1)。输出 JSON:{\"facts\":[{\"fact\":\"...\",\"importance\":0.8}]}"},
                {"role": "user", "content": json.dumps(user_msgs, ensure_ascii=False)}])
            data = json.loads(resp[resp.index("{"):resp.rindex("}")+1])
            await self.ltm.add_facts(user_id, data.get("facts", []))
        except Exception:
            # 提炼失败不影响主对话流程，直接吞掉异常
            pass

    async def _summarize_history(self, history):
        """统一摘要流程：把历史对话压缩成 Planner 可读的摘要（业务无关）。

        用户消息保留原文；助手消息（结构化结果 JSON）统一用 LLM 摘要成一句话，
        同时提取其中提到的关键项（供「换一批」时避免重复）。返回 (摘要文本, 关键项列表)。
        """
        parts = []
        mentioned = set()
        for h in history:
            if h["role"] == "user":
                parts.append(f"用户: {h['content']}")
            else:
                try:
                    data = json.loads(h["content"])
                    summary, items = await self._llm_summarize(data)
                    parts.append(f"助手: {summary}")
                    mentioned.update(items)
                except Exception:
                    parts.append(f"助手: {h['content'][:100]}")
        return "\n".join(parts), list(mentioned)

    async def _llm_summarize(self, result: dict) -> tuple[str, list[str]]:
        """统一用 LLM 摘要业务结果 + 提取关键项（业务无关，一次调用）。

        不依赖任何具体业务结构，加新业务时摘要流程零改动。
        """
        try:
            resp = await self.llm.chat(
                [{"role": "system", "content": "对下面的结构化结果做两件事：1) 摘要成一句话（100字内，说明它是什么、有几个）2) 提取其中提到的关键项名（如景点名、商品名、事项名）。严格输出 JSON：{\"summary\":\"...\",\"items\":[\"...\"]}"},
                 {"role": "user", "content": json.dumps(result, ensure_ascii=False)[:2000]}],
                response_format={"type": "json_object"})
            data = self._parse_json(resp["content"])
            if isinstance(data, dict):
                return data.get("summary", "已生成结果"), data.get("items", [])
            return "已生成结果", []
        except Exception:
            return "已生成结果", []

    def _parse_json(self, text):
        """从 LLM 输出中提取首个完整 JSON 对象，忽略尾随文本；失败时兜底把原文塞进 raw 字段。"""
        start = text.find("{")
        if start == -1:
            return {"raw": text}
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj
        except json.JSONDecodeError:
            return {"raw": text}

    async def _enrich_plan(self, final):
        """后处理：并行用名称重查真实坐标与图片，覆盖规划 Agent 编造的 location/photo。

        规划 Agent 生成 JSON 时，景点的 location、以及景点/酒店/美食的 photo 都是 LLM
        按文本中转编造或留空的，这里对行程里每个景点/酒店/美食用名称走 text_search +
        search_detail 拿真实坐标与图片覆盖；查不到的项保留原值。
        """
        city = final.get("city", "")
        # 收集所有需要补全的项：景点补坐标+图片，酒店/美食只补图片（标记 need_location）
        items = []
        for day in final.get("days", []):
            for a in day.get("attractions", []):
                items.append((a, True))
            hotel = day.get("hotel")
            if hotel:
                items.append((hotel, False))
            for m in day.get("meals", []):
                items.append((m, False))

        # 信号量限制并发：过多并发调用 MCP 会导致部分请求失败（streamable HTTP 连接限制），
        # 限到 3 个并发，既保证成功率又不过度拖慢
        sem = asyncio.Semaphore(3)

        async def fill_one(item, need_location):
            async with sem:
                name = item.get("name", "")
                if not name:
                    return
                detail = await self.ds.resolve_poi(name, city)
                if not detail:
                    return
                # 景点需要真实坐标（地图 marker 定位）
                if need_location and detail.get("location") and detail["location"].get("lng"):
                    item["location"] = {"lng": detail["location"]["lng"], "lat": detail["location"]["lat"]}
                # 所有项都补图片（原图 URL 为空时才覆盖，避免覆盖已存在的）
                if detail.get("photo") and not item.get("photo"):
                    item["photo"] = detail["photo"]

        # 并行补全（信号量限流，避免过多并发导致 MCP 请求失败）
        await asyncio.gather(*(fill_one(item, nl) for item, nl in items))
        # 补全坐标后，计算相邻景点间的通行方式与耗时（步行/打车/公交）
        await self._enrich_transit(final)
        return final

    async def _enrich_transit(self, final):
        """后处理：计算相邻景点（含酒店→首景点）间的通行方式与耗时。"""
        for day in final.get("days", []):
            attractions = day.get("attractions", [])
            for i in range(len(attractions)):
                prev_name = day.get("hotel", {}).get("name") if i == 0 else attractions[i - 1]["name"]
                cur = attractions[i]
                cur_name = cur.get("name", "")
                if not prev_name or not cur_name:
                    continue
                transit = await self._calc_transit(prev_name, cur_name)
                if transit:
                    cur["transit"] = transit

    async def _calc_transit(self, from_name, to_name):
        """算两点间通行方式与耗时：步行距离<1.5km，1.5~5km 打车，>5km 公交。"""
        try:
            route = await self.ds.plan_route(from_name, to_name, "walking")
            mins = km = None
            try:
                mins = int(float(str(route.get("duration", 0)))) // 60
            except (ValueError, TypeError):
                pass
            try:
                km = float(str(route.get("distance", 0))) / 1000
            except (ValueError, TypeError):
                pass
            if not mins or mins <= 0:
                return None
            if km is not None and km < 1.5:
                return f"步行约 {mins} 分钟"
            elif km is not None and km < 5:
                return f"打车约 {mins} 分钟"
            return f"公交约 {mins} 分钟"
        except Exception:
            return None


# 进程级单例：路由层共用同一个 service，避免重复装配/重复创建 LLM 与数据源客户端
service = AgentService()
