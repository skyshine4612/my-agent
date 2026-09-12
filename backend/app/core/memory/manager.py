# app/core/memory/manager.py
# 记忆管理器：统一编排短期记忆（组装/压缩/写回）与长期记忆（提炼/去重），
# 把记忆相关逻辑从 AgentService 抽出，AgentService 只持有 store 并委托编排。
import asyncio
import json
import logging

from app.config import settings
from app.core.token_utils import estimate_tokens

logger = logging.getLogger(__name__)

# 长期记忆去重的余弦相似度阈值：同一偏好的不同措辞一般能到 0.85 以上，
# 阈值再低就容易把「喜欢自然风光」和「喜欢历史古迹」并成一条。
_DEDUP_THRESHOLD = 0.85


def _expand_record(rec):
    """把短期记忆表的一条记录展开成 OpenAI 消息列表。

    assistant 的 content 若是 JSON 且含 tool_calls 键，展开成
    [assistant(tool_calls), tool×N(空串占位)]——tool 结果不存，动态生成空串占位配对；
    其余记录返回单条消息。
    """
    role = rec["role"]
    if role == "assistant":
        content = rec["content"]
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, dict) and "tool_calls" in data:
            calls = data["tool_calls"]
            msgs = [{"role": "assistant", "content": None, "tool_calls": calls}]
            for tc in calls:
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": ""})
            return msgs
        return [{"role": "assistant", "content": content}]
    return [{"role": role, "content": rec["content"]}]


def _record_from_msg(m):
    """把 OpenAI 消息转成短期记忆表记录（写回用），_expand_record 的逆操作（tool 占位不落表）。"""
    if m["role"] == "assistant" and m.get("tool_calls"):
        return {"role": "assistant",
                "content": json.dumps({"tool_calls": m["tool_calls"]}, ensure_ascii=False),
                "tool_call_id": None}
    return {"role": m["role"], "content": m.get("content") or "", "tool_call_id": None}


def _serialize_for_summary(msgs):
    """把早期回合的结构化消息序列化成纯文本（供摘要蒸馏）：tool_calls 保留、tool 结果跳过。"""
    parts = []
    for m in msgs:
        role = m.get("role")
        if role == "user":
            parts.append(f"[用户] {m.get('content', '')}")
        elif role == "assistant" and m.get("tool_calls"):
            names = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments", "") or ""
                try:
                    args = json.dumps(json.loads(args), ensure_ascii=False)
                except (json.JSONDecodeError, TypeError):
                    pass
                names.append(f"{fn.get('name', '')}({args})")
            parts.append("[工具调用] " + "、".join(names))
        elif role == "assistant":
            parts.append(f"[助手] {m.get('content', '')}")
        # role == "tool" 跳过：结果不存短期记忆（content 空串占位），摘要只保留 tool_calls
    return "\n\n".join(parts)


def _msg_cost(m):
    """估算单条结构化消息的 token 成本（用于回合预算切分）。"""
    return estimate_tokens(json.dumps(m, ensure_ascii=False, default=str))


class MemoryManager:
    """记忆管理器：持有会话历史/短期/长期记忆/工具结果地址索引，封装记忆的组装与提炼编排。

    四个 store 由 AgentService 创建后传入（与 AgentService 共享同一实例），
    这里只做编排，不负责 store 的创建。
    """

    def __init__(self, conversation_store, short_term_memory, long_term_memory, tool_result_store, model):
        self.conversation_store = conversation_store
        self.short_term_memory = short_term_memory
        self.long_term_memory = long_term_memory
        self.tool_result_store = tool_result_store
        self.model = model
        # 长期记忆提炼的 per-user 锁：fire-and-forget 提炼按用户串行，避免并发写长期记忆 + 重复提炼
        self._extract_locks: dict[str, asyncio.Lock] = {}

    async def delete_conversation(self, user_id, conv_id):
        """删除会话及其消息，并清理该会话的工具结果地址索引；归属校验失败返回 False。

        短期记忆随 conversations 外键级联删除，但 spill 文件在磁盘上、级联删不到，需显式清理。
        """
        ok = await self.conversation_store.delete_conversation(user_id, conv_id)
        if ok:
            await self.tool_result_store.delete_conversation(conv_id, user_id)
        return ok

    async def append_turn(self, user_id, conv_id, user_message, answer, rounds=None):
        """把本轮完整回合写进短期记忆表：user + 每轮 assistant(tool_calls) + assistant(答案)。

        rounds 是 ReAct 每轮的 tool_calls 列表（[[tc, ...], ...]），按轮次各存一条 assistant(tool_calls)；
        tool 结果与 tool 占位消息都不落表，喂 LLM 时由 _expand_record 动态生成空串占位配对。
        """
        records = [{"role": "user", "content": user_message, "tool_call_id": None}]
        for calls in (rounds or []):
            if not calls:
                continue
            records.append({"role": "assistant",
                            "content": json.dumps({"tool_calls": calls}, ensure_ascii=False),
                            "tool_call_id": None})
        records.append({"role": "assistant", "content": answer, "tool_call_id": None})
        await self.short_term_memory.append_records(user_id, conv_id, records)

    async def assemble_history(self, user_id, conv_id):
        """组装短期记忆（summary 摘要 + 结构化消息序列），返回 (messages, summary)。

        从 short_term_memory 表读（短期记忆权威源，不依赖 messages 表），分离 summary 与消息序列；
        超预算时按「整回合」压缩早期回合（序列化 + 蒸馏累积摘要 + 物理删除早期消息）。
        """
        budget = int(settings.llm_context_budget * settings.history_budget_ratio)
        records = await self.short_term_memory.get_records(user_id, conv_id)
        old_summary = next((r["content"] for r in records if r["role"] == "summary"), None)
        raw_msgs = []
        for r in records:
            if r["role"] == "summary":
                continue
            raw_msgs.extend(_expand_record(r))
        # 按「回合」分组（user 开新回合，后续 assistant/tool 归入），预算边界落在回合之间
        turns = []
        for m in raw_msgs:
            if m["role"] == "user" or not turns:
                turns.append([])
            turns[-1].append(m)
        # 反序遍历回合（最新→最老），按整回合累积预算；至少保留最新一个回合
        used = 0
        keep_from = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            cost = sum(_msg_cost(m) for m in turns[i])
            if used + cost <= budget or i == len(turns) - 1:
                used += cost
                keep_from = i
            else:
                break
        recent = [m for turn in turns[keep_from:] for m in turn]
        early = [m for turn in turns[:keep_from] for m in turn]
        summary = old_summary
        # 读到的都是未压缩消息，early 全部是新增待压缩的
        if early:
            logger.info("[memory] 历史超预算，压缩 %d 个早期回合", len(turns[:keep_from]))
            compressed_text = _serialize_for_summary(early)
            await self.tool_result_store.append_history(conv_id, user_id, compressed_text)
            summary = await self._update_summary(old_summary, compressed_text)
            # 写回：summary + 剩余消息（物理删除早期回合，删除即增量，无需 marker）
            write_records = []
            if summary:
                write_records.append({"role": "summary", "content": summary, "tool_call_id": None})
            write_records.extend(_record_from_msg(m) for m in recent if m["role"] != "tool")
            await self.short_term_memory.replace_records(user_id, conv_id, write_records)
        return recent, summary

    async def extract_facts(self, user_id, user_message):
        """异步提炼长期记忆：只从本轮 user 发言提取跨会话可复用偏好，并做语义去重。

        按 user 串行（per-user 锁），只提炼本轮这一条 user 消息；反复出现的偏好靠去重时 importance 提升来强化。
        """
        lock = self._extract_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                logger.info("[memory] 开始提炼长期记忆 user=%s", user_id)
                resp = await self.model.ainvoke([
                    {"role": "system",
                     "content": "从这条用户发言里，只提取【稳定的、跨会话可复用的】偏好或事实（如常住城市、长期的兴趣偏好、习惯性要求，例：'偏好经济型酒店'、'喜欢自然风光'）。不要提取一次性的行程请求参数（如'去成都玩2天预算2000'这种某次旅行的具体目的地/天数/预算），也不要提取助手推荐的内容。没有稳定偏好就输出空列表。每条 fact 不超过 100 个字。每条打 importance(0~1)。输出 JSON:{\"facts\":[{\"fact\":\"...\",\"importance\":0.8}]}"},
                    {"role": "user", "content": user_message}])
                text = resp.content if isinstance(resp.content, str) else ""
                data = json.loads(text[text.index("{"):text.rindex("}") + 1])
                new_facts = data.get("facts", [])
                if not new_facts:
                    return
                to_add = []
                for nf in new_facts:
                    fact_text = nf.get("fact", "")
                    importance = nf.get("importance", 0.5)
                    # 语义去重：检索最相近的已有 fact，相似度超阈值就提升其 importance（取较大值），否则算新增
                    matched = await self._find_similar(user_id, fact_text)
                    if matched:
                        await self.long_term_memory.update_importance(user_id, matched["fact"],
                                                                      max(matched["importance"], importance))
                    else:
                        to_add.append(nf)
                if to_add:
                    await self.long_term_memory.add_facts(user_id, to_add)
                logger.info("[memory] 长期记忆提炼完成：新增 %d 条", len(to_add))
            except Exception as e:
                # 提炼失败不影响主对话流程，记录 warning 便于发现数据质量回归
                # （如 LLM 返回字符串型 importance 触发 max(float, str) 的 TypeError 也会走到这里）
                logger.warning("[memory] 提炼长期记忆失败 user=%s：%s", user_id, e)

    async def _find_similar(self, user_id, fact_text):
        """在已有长期记忆里找与 fact_text 语义最近的一条，相似度不到阈值则返回 None。

        检索失败一律按「新增」处理：最坏是多存一条近似事实，
        比抛异常让整轮提炼失败轻得多（长期记忆允许降级，不依赖 embedding 可用）。
        """
        try:
            hits = await self.long_term_memory.search(user_id, fact_text, top_k=1)
        except Exception as e:
            logger.warning("[memory] 去重检索失败，本条按新增处理：%s", e)
            return None
        if hits and hits[0]["similarity"] >= _DEDUP_THRESHOLD:
            return hits[0]
        return None

    async def _update_summary(self, old_summary, compressed_text):
        """LLM 把「旧摘要 + 更早对话」浓缩成新的累积摘要；失败回退字符串拼接。

        完整传入 compressed_text、不做截断，避免关键数据（硬数据）被静默丢弃。
        """
        try:
            resp = await self.model.ainvoke([
                {"role": "system",
                 "content": "把下面的「旧摘要 + 更早对话」浓缩成一个新的累积摘要（100-300 字），保留核心结论、关键数据（菜名/价格/时间/结构要点），以及调用了哪些工具、查了什么参数（如「菜谱做法(宫保鸡丁)」）。文件名必须原样保留（如「测试数据.xlsx」），不要概括成「某个 Excel 文件」——那是重跑脚本的唯一指针。"},
                {"role": "user",
                 "content": f"旧摘要：{old_summary or '（无）'}\n\n更早对话：\n{compressed_text}"},
            ])
            return resp.content if isinstance(resp.content, str) else ""
        except Exception:
            logger.warning("[memory] 摘要浓缩失败，回退拼接")
            return (old_summary or "") + "\n" + compressed_text
