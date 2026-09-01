# app/core/agent.py
# Agent 基类：实现 ReAct 推理-行动循环（LLM 决策 → 工具调用 → 回填结果 → 再决策），
# 工作记忆的 token 预算淘汰委托给 core/memory/working.py 的 WorkingMemory。
import asyncio
import json
import logging
from dataclasses import dataclass, field

from app.config import settings
from app.core.context import current_user_id
from app.core.memory.working import WorkingMemory

logger = logging.getLogger(__name__)

# 不写短期记忆的工具：get_skill 返回「规则正文」而非查询结果，call_sub_agent 返回委派摘要
# （子任务内部已按子会话隔离写短期记忆）。它们不需要「防重查」，写进去反而产生无意义摘要。
NON_SHORT_TERM_TOOLS = {"get_skill", "call_sub_agent"}
# 非查询结果工具：返回「读回片段/规则正文/委派摘要」而非「查询结果」。
# 它们的返回原样回填窗口（不结构化截断），也不触发地址索引（不写文件）——
# 否则 read_file 读回的片段又被截断/写文件，会形成 read→截断/spill→read 的循环。
NON_QUERY_TOOLS = {"read_file", "grep", "get_skill", "call_sub_agent"}


def _struct_truncate_to_str(raw, list_limit=15, dict_limit=20, str_limit=4000):
    """结构化截断：list 取前 list_limit 条 / dict 取前 dict_limit 键 / str 截断 str_limit。

    返回 (截断文本, 截断描述)：截断描述如「仅展示前15条（共20条）」或「已截断」，未截断时为空串。
    由调用方决定如何拼提示（spill 时合并成一句话，无 spill 时单独加标记）。
    """
    note = ""
    if isinstance(raw, list):
        kept = raw[:list_limit]
        s = json.dumps(kept, ensure_ascii=False, default=str)
        if len(raw) > list_limit:
            note = f"仅展示前{list_limit}条（共{len(raw)}条）"
    elif isinstance(raw, dict):
        keys = list(raw.keys())
        kept = {k: raw[k] for k in keys[:dict_limit]}
        s = json.dumps(kept, ensure_ascii=False, default=str)
        if len(keys) > dict_limit:
            note = f"仅展示前{dict_limit}键（共{len(keys)}键）"
    else:
        s = str(raw)
    # 最终仍超过 str_limit 则硬截断兜底（dict/list 展开后可能超长）
    if len(s) > str_limit:
        s = s[:str_limit]
        if not note:
            note = "已截断"
    return s, note


def _needs_spill(raw, list_limit=15, dict_limit=20, str_limit=4000) -> bool:
    """判断完整结果是否被结构化截断（触发地址索引落库的条件）。

    与 _struct_truncate_to_str 的截断维度对齐：list 超 list_limit / dict 超 dict_limit 键 /
    str 超 str_limit 都算「被截断」，需要写文件保存完整结果供 read_file/grep 读回。
    """
    if isinstance(raw, list):
        return len(raw) > list_limit or len(str(raw)) > str_limit
    if isinstance(raw, dict):
        return len(raw) > dict_limit or len(str(raw)) > str_limit
    return len(str(raw)) > str_limit


@dataclass
class Agent:
    """Agent 基类：把 LLM 推理与工具执行编排为 ReAct「思考-行动-观察」循环。

    属性：
        name:            Agent 名称（用于日志与追踪）
        system_prompt:   系统提示词，注入每轮对话的最前
        tools:           本 Agent 可调用的工具名列表
        llm:             LLM 客户端（实现 LLMClient 接口，含 chat / complete / stream_chat）
        max_iters:       单次 run_stream 的 ReAct 最大迭代轮数，防止无限循环
    """
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    llm: object = None
    max_iters: int = 20

    async def run_stream(self, user_message, history, registry, on_event=None,
                         short_term_memory=None, conversation_id=None, context_budget=None,
                         tool_result_store=None):
        """以流式方式执行一次完整 ReAct 循环，返回最终累积文本答复。

        流程：
            - 通过 llm.stream_chat 逐 token 产出；调工具轮次的 content 是内部思考（thought），不上发前端；
            - 仅最终答案（不调工具的轮次）经 on_event 发为 token 事件；
            - 同一轮多个 tool_calls 用 asyncio.gather 并行执行，结果按原始顺序回填 tool 消息；
            - 工具结果回填前做结构化截断（list 前15 / dict 前20键 / str 4000），
              tool_result 事件额外携带 visible（模型工作记忆里的截断版结果），供上层 critic 校验，
              保证 critic 校验所用语料与模型生成答案时所见一致；
            - 每轮前查短期记忆拼 short_term_hint 注入，防止重复查询已查过的工具；
            - 每轮后工作记忆按 token 预算淘汰最老交互（超预算时 LLM 蒸馏成摘要兜底）。

        short_term_memory：短期记忆对象（ShortTermMemory），为 None 时不写/不注入。
        conversation_id：  短期记忆隔离维度（主会话或子会话 id）。
        context_budget：   工作记忆 token 预算，为 None 时不淘汰（保持原有无限累积行为）。
        tool_result_store：工具结果地址索引存储（ToolResultStore），为 None 时不落库（不启用地址索引）。

        on_event：可选异步回调，接收流式事件（token / tool_call / tool_result）；为 None 时静默。
        """

        async def emit(event):
            """向回调上报一个流式事件（无回调则静默跳过）。"""
            if on_event is not None:
                await on_event(event)

        logger.info("[Agent:%s] 开始执行（流式），输入：%.60s", self.name, user_message)
        # 工作记忆（预算淘汰器）：传入 llm 与预算时启用，否则禁用（不淘汰）
        wm = WorkingMemory(self.llm, context_budget) if context_budget else None
        # 固定头：system 提示词（已含 long_term_hint），永不淘汰
        fixed = [{"role": "system", "content": self.system_prompt}]
        # 可变窗口：历史对话 + ReAct 工具交互，超预算淘汰最老
        window = list(history)
        # 当前用户指令：每轮拼在窗口之后，保持最新、不被淘汰
        user_msg = {"role": "user", "content": user_message}
        # 若本 Agent 声明了工具，则把工具名列表转成 OpenAI 工具 schema，供 LLM 决策是否调用
        tool_schemas = registry.to_openai_schemas(self.tools) if self.tools else None
        # 累积文本：只累积最终答案（不调工具轮次的 content）；调工具的思考（thought）是内部推理，
        # 不计入回答、也不上发前端，避免把 agent 的自言自语当答案展示给用户。
        full_text = ""
        # 进入 ReAct 循环：最多迭代 max_iters 轮，每轮完成一次「思考 → 行动 → 观察」
        for i in range(self.max_iters):
            # 每轮前：查短期记忆拼 short_term_hint（system 消息，每轮重建、不累积）
            short_term_hint = await self._build_short_term_hint(short_term_memory, conversation_id)
            messages = fixed + short_term_hint + window + [user_msg]
            round_text = ""
            calls = None
            status_sent = False
            # 流式调 LLM：content 只累积到 round_text，不即时上发——只有确认本轮不再调工具时，
            # 其内容才是面向用户的最终答案；调工具的轮次 content 是 thought，不上发前端。
            async for event in self.llm.stream_chat(messages, tool_schemas):
                if event.get("type") == "content":
                    round_text += event.get("text", "")
                    # LLM 开始流式产出（思考或答案）时发一次 status，让前端显示「整理中」，覆盖生成期的长静默
                    if not status_sent:
                        await emit({"type": "status", "status": "generating"})
                        status_sent = True
                elif event.get("type") == "end":
                    calls = event.get("tool_calls")
            # 无工具调用：本轮是最终答案，按小块上发（前端逐块渲染，保持流式感）并返回
            if not calls:
                full_text += round_text
                for j in range(0, len(round_text), 40):
                    await emit({"type": "token", "content": round_text[j:j + 40]})
                logger.info("[Agent:%s] 本轮完成，返回最终答案（%d 字）", self.name, len(full_text))
                return full_text
            # 有工具调用：把本轮 assistant 回复（含 tool_calls）写入窗口，让后续轮次能看见完整上下文
            window.append({"role": "assistant", "content": round_text, "tool_calls": calls})
            logger.info("[Agent:%s] 第 %d 轮决定调用 %d 个工具：%s", self.name, i + 1, len(calls),
                        ", ".join(tc["function"]["name"] for tc in calls))

            # 并行执行本轮所有无依赖工具调用；gather 保持与 calls 相同的返回顺序
            async def run_tool(tc):
                """执行单个工具调用，上报 tool_call / tool_result 事件并返回回填项。

                tool_call 与 tool_result 事件都携带 tc["id"]（工具调用唯一 id），供上层精确配对。
                并行执行时工具按「完成先后」返回，tool_result 顺序与 tool_call 顺序并不一致，
                故配对必须依赖 id 而非工具名或队列顺序，否则同名工具并发会错配 summary。
                """
                fn = tc["function"]
                # 解析工具入参（JSON 字符串 → dict），入参缺失时兜底为空对象
                args = json.loads(fn["arguments"] or "{}")
                logger.info("[Agent:%s] 第 %d 轮 调工具 %s（id=%s），参数 %s", self.name, i + 1, fn["name"], tc["id"],
                            json.dumps(args, ensure_ascii=False))
                await emit({"type": "tool_call", "agent": self.name, "tool": fn["name"], "args": args, "id": tc["id"],
                            "label": registry.label(fn["name"])})
                # 通过 registry 真正执行工具，拿到结构化原始结果（call_raw 不做 str() 转换）；
                # 失败时把错误回传给 LLM 而非让整个 run_stream 崩溃（MCP 限流/网络抖动会抛异常）。
                try:
                    raw = await registry.call_raw(fn["name"], args)
                except Exception as e:
                    logger.warning("[Agent:%s] 工具 %s 执行失败：%s", self.name, fn["name"], e)
                    raw = f"工具执行失败：{e}"
                # 回填工作记忆：结构化截断（list 前15 / dict 前20键 / str truncate_limit），
                # 这份截断版结果就是模型所见语料；critic 校验与修正重写都复用它，保证语料一致。
                # 放宽截断避免丢数据（天气 7 天、POI 前 15 个都保留），超长由 token 预算淘汰兜底
                if fn["name"] in NON_QUERY_TOOLS:
                    # 非查询结果工具（read_file/grep/get_skill/call_sub_agent）：返回内容原样回填，
                    # 不做结构化截断（读回片段/规则正文本身就是有界且需要完整的），也不触发地址索引
                    text = str(raw)
                else:
                    text, note = _struct_truncate_to_str(raw, 15, 20, settings.truncate_limit)
                    # 地址索引：完整结果被截断（list 超15 / dict 超20键 / str 超阈值）时写临时文件，
                    # 窗口只放预览 + 文件路径，模型可 read_file/grep 读回被省略部分
                    if (tool_result_store is not None and conversation_id is not None
                            and _needs_spill(raw, str_limit=settings.truncate_limit)):
                        path = await tool_result_store.write(conversation_id, current_user_id.get(), str(raw))
                        parts = [note, f"完整结果存到 {path}"] if note else [f"完整结果存到 {path}"]
                        text = f"{text}\n[{'; '.join(parts)}，可用 read_file/grep 读回剩余]"
                    elif note:
                        text = f"{text}[{note}]"
                # 写短期记忆：一句话级 summary（结构化截断为主，大结果 LLM 精炼），供后续轮次防重查。
                # get_skill / call_sub_agent 这类「非查询结果」工具不写，避免把规则正文/委派摘要误当已查结果
                if short_term_memory is not None and conversation_id is not None and fn["name"] not in NON_SHORT_TERM_TOOLS:
                    summary = await self._summarize_for_short_term(raw)
                    await short_term_memory.write(conversation_id, fn["name"], args, summary)
                    logger.info("[Agent:%s] 写短期记忆：%s(%s)", self.name, fn["name"], json.dumps(args, ensure_ascii=False))
                logger.info("[Agent:%s] 第 %d 轮 工具 %s 完成（id=%s），返回 %.120s", self.name, i + 1, fn["name"], tc["id"], text)
                await emit({"type": "tool_result", "agent": self.name, "tool": fn["name"],
                            "status": True, "result": text, "id": tc["id"], "label": registry.label(fn["name"])})
                # 返回带 tool_call_id 的回填项，供上层按原始顺序组装 tool 消息
                return {"tool_call_id": tc["id"], "content": text}

            results = await asyncio.gather(*(run_tool(tc) for tc in calls))
            # 按 tool_calls 原始顺序回填 tool 消息：gather 已保序，这里按序追加即保证 tool_call_id 对齐
            for r in results:
                window.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})
            # 每轮后：工作记忆按 token 预算淘汰最老交互（超预算时成对淘汰 + LLM 摘要兜底）
            if wm is not None:
                window = await wm.fit(window)
        # 达到 max_iters 仍未收敛（每轮都在调工具）时兜底返回：
        # 有累积文本就用累积文本，否则给一句非空提示，避免最终输出空串
        if full_text:
            return full_text
        return "抱歉，本轮查询了较多信息但未能整合成完整回答，请换个方式或精简需求后再试。"

    async def _build_short_term_hint(self, short_term_memory, conversation_id):
        """查短期记忆拼 short_term_hint（极简清单 system 消息）；无短期记忆/会话/记录时返回空列表。

        每轮重建、不累积：只反映「当前已查清单」，防止 LLM 反复重查已查过的工具。
        """
        if short_term_memory is None or conversation_id is None:
            return []
        rows = await short_term_memory.get_all(conversation_id)
        if not rows:
            return []
        logger.info("[Agent:%s] 注入短期记忆 hint：%d 条已查工具", self.name, len(rows))
        # 拼极简清单：tool_name(args) → 一句话结果（明确标注为摘要，避免模型误当完整结果直接引用）
        items = [f"{r['tool_name']}({r['args']}) → {r['summary']}" for r in rows]
        return [{"role": "system", "content": "已查（以下仅为摘要、非完整结果；追问具体内容或询问新的具体实体都需重新查）：" + "；".join(items)}]

    async def _summarize_for_short_term(self, raw):
        """生成短期记忆的一句话级 summary：结构化截断为主，大结果（>500 字符）LLM 精炼成一句话。

        防重查的核心是 tool_name+args（write 时完整保留），summary 只做「结果要点」补充，
        故小结果直接用结构化截断省成本，只有多班次/多航班等大结果才花一次 LLM 精炼。
        """
        raw_str = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
        # 小结果：直接结构化截断（list 前2 / dict 前3键 / str 100）
        if len(raw_str) <= 500:
            return _struct_truncate_to_str(raw, 2, 3, 100)[0]
        # 大结果：LLM 精炼成一句话，失败退回结构化截断（不影响主流程）
        try:
            return await self.llm.complete([
                {"role": "system",
                 "content": "把下面的工具返回结果精炼成一句话摘要，保留关键信息（班次/时间/价格、天气/温度、名称等）。"},
                {"role": "user", "content": raw_str[:2000]},
            ])
        except Exception:
            logger.warning("[Agent:%s] LLM 精炼短期记忆摘要失败，退回结构化截断", self.name)
            return _struct_truncate_to_str(raw, 2, 3, 100)[0]
