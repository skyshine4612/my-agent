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
        max_iters:       单次 run 的 ReAct 最大迭代轮数，防止无限循环
    """
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    llm: object = None
    max_iters: int = 10
    json_mode: bool = False   # 是否强制 JSON 输出（规划 Agent 等纯 JSON 生成场景）

    async def run(self, user_message, history, registry, on_step=None):
        """执行一次完整的 ReAct 循环，返回最终文本答复。

        流程：拼 system+history+user → 调 LLM → 若有 tool_calls 则逐个执行并回填 tool 消息
        → 再调 LLM → 直到无 tool_calls 或超过 max_iters。

        on_step：可选异步回调，接收每个中间步骤事件（dict，含 type 字段），
        用于把「思考 → 行动 → 观察」过程上报（如经 SSE 推给前端展示思考过程）。
        """
        async def emit(event):
            """向回调上报一个步骤事件（无回调则静默跳过）。"""
            if on_step is not None:
                await on_step(event)

        # 日志：Agent 开始执行，记录输入摘要，便于追踪每个子 Agent 干了什么
        logger.info("[Agent:%s] 开始执行，输入：%.60s", self.name, user_message)
        await emit({"type": "agent_start", "agent": self.name})
        # 1. 组装初始消息：system 提示词放最前，其次拼接历史对话，最后追加本轮用户输入
        messages = [{"role": "system", "content": self.system_prompt}] + history
        messages.append({"role": "user", "content": user_message})
        # 2. 若本 Agent 声明了工具，则把工具名列表转成 OpenAI 工具 schema，供 LLM 决策是否调用
        tool_schemas = registry.to_openai_schemas(self.tools) if self.tools else None
        # 3. 进入 ReAct 循环：最多迭代 max_iters 轮，每轮完成一次「思考 → 行动 → 观察」
        for i in range(self.max_iters):
            # 3.1 调 LLM：传入完整消息与工具 schema，模型返回文本（content）或工具调用（tool_calls）
            # json_mode 时强制模型只输出合法 JSON（从源头避免解析失败）
            resp = await self.llm.chat(messages, tool_schemas,
                                       response_format={"type": "json_object"} if self.json_mode else None)
            # 3.2 取出模型本次返回的工具调用列表
            calls = resp.get("tool_calls")
            # 3.3 无工具调用：说明模型已得出最终答案，直接返回其文本内容并结束循环
            if not calls:
                logger.info("[Agent:%s] 完成，返回文本（%d 字）", self.name, len(resp.get("content", "")))
                await emit({"type": "agent_done", "agent": self.name,
                            "summary": resp.get("content", "")[:120]})
                return resp.get("content", "")
            # 3.4 有工具调用：把本轮 assistant 回复（含 tool_calls）写入消息，让后续轮次能看见完整上下文
            messages.append({"role": "assistant", "content": resp.get("content") or "", "tool_calls": calls})
            logger.info("[Agent:%s] 第 %d 轮决定调用 %d 个工具", self.name, i + 1, len(calls))
            await emit({"type": "agent_think", "agent": self.name,
                        "round": i + 1, "tool_count": len(calls)})
            # 3.5 逐个执行工具调用，并把结果作为 tool 消息回填，形成「行动 → 观察」闭环
            for tc in calls:
                fn = tc["function"]
                # 解析工具入参（JSON 字符串 → dict），入参缺失时兜底为空对象
                args = json.loads(fn["arguments"] or "{}")
                # 日志：记录本次工具调用的名称与参数
                logger.info("[Agent:%s] 调工具 %s，参数 %s", self.name, fn["name"], json.dumps(args, ensure_ascii=False))
                await emit({"type": "tool_call", "agent": self.name, "tool": fn["name"], "args": args})
                # 通过 registry 真正执行工具，拿到观察结果
                out = await registry.call(fn["name"], args)
                # 日志：记录工具返回结果摘要（截断，避免日志过大）
                logger.info("[Agent:%s] 工具 %s 返回 %.120s", self.name, fn["name"], str(out))
                await emit({"type": "tool_result", "agent": self.name, "tool": fn["name"],
                            "summary": str(out)[:120]})
                # 用 tool_call_id 关联对应调用，把观察结果回填为 tool 消息
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(out)})
        # 4. 达到 max_iters 仍未收敛（每轮都在调工具）时，兜底返回最后一轮的文本内容（通常为空）
        return resp.get("content", "")

    async def run_stream(self, user_message, history, registry, on_event=None):
        """以流式方式执行一次完整 ReAct 循环，返回最终累积文本答复。

        与 run() 的区别：
            - 通过 llm.stream_chat 逐 token 产出，内容增量即时经 on_event 发为 token 事件；
            - 同一轮多个 tool_calls 用 asyncio.gather 并行执行，结果按原始顺序回填 tool 消息；
            - 工具结果回填前按 4000 字符截断并追加 [已截断] 标记（提示 LLM 结果不全可重查）；
            - 组装消息前先用 WorkingMemory 压缩 history（system prompt 与当前 user 指令不参与蒸馏）。

        on_event：可选异步回调，接收流式事件（token / tool_call / tool_result 等）；
        为 None 时静默（子 Agent 被编排调用时只需最终文本，无需向前端发事件）。
        """
        async def emit(event):
            """向回调上报一个流式事件（无回调则静默跳过）。"""
            if on_event is not None:
                await on_event(event)

        logger.info("[Agent:%s] 开始执行（流式），输入：%.60s", self.name, user_message)
        await emit({"type": "agent_start", "agent": self.name})
        # 1. 压缩历史：只蒸馏 history（历史对话），system prompt（身份/规则）与当前 user 指令保留原样
        history = await WorkingMemory(self.llm).fit(history)
        # 2. 组装消息：system 提示词放最前，其次拼接压缩后的历史，最后追加本轮用户输入
        messages = [{"role": "system", "content": self.system_prompt}] + history
        messages.append({"role": "user", "content": user_message})
        # 3. 若本 Agent 声明了工具，则把工具名列表转成 OpenAI 工具 schema，供 LLM 决策是否调用
        tool_schemas = registry.to_openai_schemas(self.tools) if self.tools else None
        # 4. 累积文本：所有轮次的流出文本（含调工具前思考）都算作回答
        full_text = ""
        # 5. 进入 ReAct 循环：最多迭代 max_iters 轮，每轮完成一次「思考 → 行动 → 观察」
        for i in range(self.max_iters):
            round_text = ""
            calls = None
            # 5.1 流式调 LLM：逐事件处理，content 增量即时上发，end 事件携带本轮工具调用
            async for event in self.llm.stream_chat(messages, tool_schemas):
                if event.get("type") == "content":
                    text = event.get("text", "")
                    round_text += text
                    full_text += text
                    await emit({"type": "token", "content": text})
                elif event.get("type") == "end":
                    calls = event.get("tool_calls")
            # 5.2 无工具调用：模型已给出最终答案，返回累积文本并结束循环
            if not calls:
                logger.info("[Agent:%s] 完成，返回文本（%d 字）", self.name, len(full_text))
                await emit({"type": "agent_done", "agent": self.name, "summary": full_text[:120]})
                return full_text
            # 5.3 有工具调用：把本轮 assistant 回复（含 tool_calls）写入消息，让后续轮次能看见完整上下文
            messages.append({"role": "assistant", "content": round_text, "tool_calls": calls})
            logger.info("[Agent:%s] 第 %d 轮决定调用 %d 个工具", self.name, i + 1, len(calls))
            await emit({"type": "agent_think", "agent": self.name, "round": i + 1, "tool_count": len(calls)})

            # 5.4 并行执行本轮所有无依赖工具调用；gather 保持与 calls 相同的返回顺序
            async def run_tool(tc):
                fn = tc["function"]
                # 解析工具入参（JSON 字符串 → dict），入参缺失时兜底为空对象
                args = json.loads(fn["arguments"] or "{}")
                logger.info("[Agent:%s] 调工具 %s，参数 %s", self.name, fn["name"], json.dumps(args, ensure_ascii=False))
                await emit({"type": "tool_call", "agent": self.name, "tool": fn["name"], "args": args})
                # 通过 registry 真正执行工具，拿到观察结果
                out = await registry.call(fn["name"], args)
                # 结果按 4000 字符截断 + [已截断] 标记：提示 LLM 结果不全、可带更精确入参重查（ReAct 自愈）
                text = str(out)
                if len(text) > 4000:
                    text = text[:4000] + "[已截断]"
                logger.info("[Agent:%s] 工具 %s 返回 %.120s", self.name, fn["name"], text)
                await emit({"type": "tool_result", "agent": self.name, "tool": fn["name"], "summary": text[:120]})
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
