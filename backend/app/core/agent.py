# app/core/agent.py
# Agent 基类与工作记忆模块：实现 ReAct 推理-行动循环（LLM 决策 → 工具调用 → 回填结果 → 再决策），
# 以及 WorkingMemory 的超长上下文摘要压缩（把最早消息蒸馏成摘要，而非简单丢弃）。
import asyncio
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


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
    max_iters: int = 10

    async def run_stream(self, user_message, history, registry, on_event=None):
        """以流式方式执行一次完整 ReAct 循环，返回最终累积文本答复。

        流程：
            - 通过 llm.stream_chat 逐 token 产出；调工具轮次的 content 是内部思考（thought），不上发前端；
            - 仅最终答案（不调工具的轮次）经 on_event 发为 token 事件；
            - 同一轮多个 tool_calls 用 asyncio.gather 并行执行，结果按原始顺序回填 tool 消息；
            - 工具结果回填前按 4000 字符截断并追加 [已截断] 标记（提示 LLM 结果不全可重查），
              但 tool_result 事件额外携带 full（截断前的完整结果），供上层 critic 校验；
            - 组装消息前先用 WorkingMemory 压缩 history（system prompt 与当前 user 指令不参与蒸馏）。

        on_event：可选异步回调，接收流式事件（token / tool_call / tool_result）；为 None 时静默。
        """
        async def emit(event):
            """向回调上报一个流式事件（无回调则静默跳过）。"""
            if on_event is not None:
                await on_event(event)

        logger.info("[Agent:%s] 开始执行（流式），输入：%.60s", self.name, user_message)
        # 1. 压缩历史：只蒸馏 history（历史对话），system prompt（身份/规则）与当前 user 指令保留原样
        history = await WorkingMemory(self.llm).fit(history)
        # 2. 组装消息：system 提示词放最前，其次拼接压缩后的历史，最后追加本轮用户输入
        messages = [{"role": "system", "content": self.system_prompt}] + history
        messages.append({"role": "user", "content": user_message})
        # 3. 若本 Agent 声明了工具，则把工具名列表转成 OpenAI 工具 schema，供 LLM 决策是否调用
        tool_schemas = registry.to_openai_schemas(self.tools) if self.tools else None
        # 4. 累积文本：只累积最终答案（不调工具轮次的 content）；调工具的思考（thought）是内部推理，
        # 不计入回答、也不上发前端，避免把 agent 的自言自语当答案展示给用户。
        full_text = ""
        # 5. 进入 ReAct 循环：最多迭代 max_iters 轮，每轮完成一次「思考 → 行动 → 观察」
        for i in range(self.max_iters):
            round_text = ""
            calls = None
            status_sent = False
            # 5.1 流式调 LLM：content 只累积到 round_text，不即时上发——只有确认本轮不再调工具时，
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
            # 5.2 无工具调用：本轮是最终答案，按小块上发（前端逐块渲染，保持流式感）并返回
            if not calls:
                full_text += round_text
                for j in range(0, len(round_text), 40):
                    await emit({"type": "token", "content": round_text[j:j + 40]})
                logger.info("[Agent:%s] 完成，返回文本（%d 字）", self.name, len(full_text))
                return full_text
            # 5.3 有工具调用：把本轮 assistant 回复（含 tool_calls）写入消息，让后续轮次能看见完整上下文
            messages.append({"role": "assistant", "content": round_text, "tool_calls": calls})
            logger.info("[Agent:%s] 第 %d 轮决定调用 %d 个工具", self.name, i + 1, len(calls))

            # 5.4 并行执行本轮所有无依赖工具调用；gather 保持与 calls 相同的返回顺序
            async def run_tool(tc):
                """执行单个工具调用，上报 tool_call / tool_result 事件并返回回填项。

                tool_call 与 tool_result 事件都携带 tc["id"]（工具调用唯一 id），供上层精确配对。
                并行执行时工具按「完成先后」返回，tool_result 顺序与 tool_call 顺序并不一致，
                故配对必须依赖 id 而非工具名或队列顺序，否则同名工具并发会错配 summary。
                """
                fn = tc["function"]
                # 解析工具入参（JSON 字符串 → dict），入参缺失时兜底为空对象
                args = json.loads(fn["arguments"] or "{}")
                logger.info("[Agent:%s] 调工具 %s，参数 %s", self.name, fn["name"], json.dumps(args, ensure_ascii=False))
                await emit({"type": "tool_call", "agent": self.name, "tool": fn["name"], "args": args, "id": tc["id"], "label": registry.label(fn["name"])})
                # 通过 registry 真正执行工具，拿到观察结果；失败时把错误回传给 LLM 而非让整个 run_stream 崩溃
                # （MCP 限流/网络抖动会导致工具抛异常，若不上抛会直接中断 SSE 流）
                try:
                    out = await registry.call(fn["name"], args)
                except Exception as e:
                    logger.warning("[Agent:%s] 工具 %s 执行失败：%s", self.name, fn["name"], e)
                    out = f"工具执行失败：{e}"
                # full 保留截断前的完整结果，供上层的 critic 回路校验回答是否与工具结果矛盾
                full = str(out)
                # 结果按 4000 字符截断 + [已截断] 标记：提示 LLM 结果不全、可带更精确入参重查（ReAct 自愈）
                text = full
                if len(text) > 4000:
                    text = text[:4000] + "[已截断]"
                logger.info("[Agent:%s] 工具 %s 返回 %.120s", self.name, fn["name"], text)
                await emit({"type": "tool_result", "agent": self.name, "tool": fn["name"],
                            "summary": text[:120], "full": full, "id": tc["id"], "label": registry.label(fn["name"])})
                # 返回带 tool_call_id 的回填项，供上层按原始顺序组装 tool 消息
                return {"tool_call_id": tc["id"], "content": text}

            results = await asyncio.gather(*(run_tool(tc) for tc in calls))
            # 5.5 按 tool_calls 原始顺序回填 tool 消息：gather 已保序，这里按序追加即保证 tool_call_id 对齐
            for r in results:
                messages.append({"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]})
        # 6. 达到 max_iters 仍未收敛（每轮都在调工具）时，兜底返回累积文本
        return full_text


class WorkingMemory:
    """工作记忆：上下文窗口 + 超长摘要压缩（蒸馏，非丢弃）。

    当对话历史超出 token 预算时，不是简单截断，而是把最早一批消息交给 LLM
    蒸馏成一段摘要，用摘要替代原消息，在压缩的同时保留关键约束/决策/结果。
    """
    def __init__(self, llm, max_tokens=80000):
        # 保存 LLM 客户端（用于蒸馏摘要）与最大 token 预算
        self.llm = llm
        self.max_tokens = max_tokens

    async def fit(self, messages):
        """压缩消息列表使其不超 token 预算，返回新的消息列表（不改动原列表）。

        超长时循环把最早一批消息蒸馏成摘要，直到估重降到预算内或消息少到无需压缩。
        """
        # 只要估重仍超预算、且剩余消息多于 2 条（需保留至少一条可蒸馏的上下文）就继续压缩
        while self._estimate(messages) > self.max_tokens and len(messages) > 2:
            # 取最早的一批消息（最多 10 条）作为本次蒸馏对象
            head = messages[:10]
            # 蒸馏前把 head 里非纯文本消息（assistant 带 tool_calls、tool 带 tool_call_id）序列化成可读文本，
            # 否则直接把这类结构喂给 complete 是非法消息结构（tool 角色缺少对应的 assistant tool_calls）
            serialized = [self._serialize_message(m) for m in head]
            # 调 LLM 把这批消息蒸馏成一段摘要，保留关键约束、决策、结果
            summary = await self.llm.complete([
                {"role": "system", "content": "把下面对话蒸馏成一段摘要，保留关键约束、决策、结果。"}] + serialized)
            # 用「早期对话摘要」系统消息替换这批原始消息：压缩后的列表 = 摘要 + 尚未蒸馏的剩余消息
            messages = [{"role": "system", "content": "[早期对话摘要] " + summary}] + messages[10:]
        return messages

    @staticmethod
    def _serialize_message(m):
        """把单条消息序列化为纯文本（保留角色），使带 tool_calls / tool_call_id 的消息可安全喂给 complete。

        返回只含 role 与 content 的 dict，剥离 tool_calls / tool_call_id 等结构化字段，
        把工具调用/工具结果改写为可读文本（如 [工具调用 name(args)] / [工具结果 ...]）。
        """
        role = m.get("role")
        content = m.get("content") or ""
        # assistant 且带 tool_calls：把文本与每个工具调用拼成可读描述
        if role == "assistant" and m.get("tool_calls"):
            parts = [str(content)] if content else []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                args = fn.get("arguments") or ""
                # arguments 本身是 JSON 字符串，尝试紧凑化便于阅读；解析失败则原样保留
                try:
                    args = json.dumps(json.loads(args), ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    pass
                parts.append(f"[工具调用 {name}({args})]")
            return {"role": "assistant", "content": " ".join(parts)}
        # tool 消息：tool 角色在无对应 assistant tool_calls 时是非法结构，改挂 user 角色并标注工具结果
        if role == "tool":
            return {"role": "user", "content": f"[工具结果 {m.get('tool_call_id', '')}] {content}"}
        # 其余角色（system / user / 纯文本 assistant）直接序列化内容
        return {"role": role, "content": str(content)}

    def _estimate(self, messages):
        """粗略估算消息列表的 token 数：按「每 4 个字符约等于 1 token」的启发式规则统计。"""
        return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4
