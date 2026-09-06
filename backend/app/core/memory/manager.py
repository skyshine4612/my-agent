# app/core/memory/manager.py
# 记忆管理器：统一编排短期记忆（组装/压缩/写回）与长期记忆（提炼/去重），
# 把记忆相关逻辑从 AgentService 抽出，AgentService 只持有 store 并委托编排。
import asyncio
import difflib
import json
import logging

from app.config import settings
from app.core.token_utils import estimate_tokens

logger = logging.getLogger(__name__)


def _similar(a: str, b: str, threshold: float = 0.7) -> bool:
    """简单字符串相似度：用 difflib.SequenceMatcher 判断两条 fact 是否语义相近（去重用）。"""
    return difflib.SequenceMatcher(None, a, b).ratio() > threshold


class MemoryManager:
    """记忆管理器：持有会话历史/短期/长期记忆/工具结果地址索引，封装记忆的组装与提炼编排。

    四个 store 由 AgentService 创建后传入（与 AgentService 共享同一实例），
    这里只做编排，不负责 store 的创建。
    """

    def __init__(self, conversation_store, short_term_memory, long_term_memory, tool_result_store, llm):
        self.conversation_store = conversation_store
        self.short_term_memory = short_term_memory
        self.long_term_memory = long_term_memory
        self.tool_result_store = tool_result_store
        self.llm = llm
        # 长期记忆提炼的 per-user 锁：fire-and-forget 提炼按用户串行，避免并发写长期记忆 + 重复提炼
        self._extract_locks: dict[str, asyncio.Lock] = {}

    async def delete_conversation(self, user_id, conv_id):
        """删除会话及其消息，并清理该会话的短期记忆与工具结果地址索引；归属校验失败返回 False。"""
        ok = await self.conversation_store.delete_conversation(user_id, conv_id)
        if ok:
            await self.short_term_memory.clear_conversation(conv_id)
            await self.tool_result_store.delete_conversation(conv_id, user_id)
        return ok

    async def append_current_turn(self, conv_id, user_message, answer):
        """把本轮 user + assistant 追加进短期记忆 recent，让「记忆」页立即显示最新一轮。

        不做预算淘汰——超预算部分由下一轮 assemble_history 重新切分时压进摘要。
        """
        records = await self.short_term_memory.get_records(conv_id)
        records.append({"role": "user", "content": user_message})
        records.append({"role": "assistant", "content": answer})
        await self.short_term_memory.replace_records(conv_id, records)

    async def assemble_history(self, user_id, conv_id):
        """组装短期记忆（running summary 摘要 + 最近几轮原文），返回 (recent_messages, summary)。

        只读 compressed_through marker 之后的消息（已压缩的早期回合不再读原文），按 token 预算切分：
        超预算的早期回合压成累积摘要（存 short_term_memory 表 + 落盘 _history.txt），
        预算内的最近几轮保留原文。返回最近几轮原文（注入对话）与摘要（注入 system）。
        """
        budget = int(settings.llm_context_budget * settings.history_budget_ratio)
        # 先读旧摘要 + 压缩截止 marker，再只读 marker 之后的消息（增量读，避免每轮全量扫 messages）
        old_records = await self.short_term_memory.get_records(conv_id)
        old_summary = next((r["content"] for r in old_records if r["role"] == "summary"), None)
        old_marker = await self.conversation_store.get_compressed_through(conv_id)
        raw_history = await self.conversation_store.get_history_since(user_id, conv_id, old_marker)
        # 还原纯文本并按「回合」分组：一个 user 消息 + 其后连续的 assistant 回复算一个回合。
        # 预算边界只落在回合之间，不把一问一答从中间切开。
        turns = []
        for h in raw_history:
            content = h["content"]
            if h["role"] == "assistant":
                data = None
                try:
                    data = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    pass
                if isinstance(data, dict) and isinstance(data.get("content"), str):
                    content = data["content"]
            if h["role"] == "user" or not turns:
                turns.append([])
            turns[-1].append({"id": h["id"], "role": h["role"], "content": content})
        # 反序遍历回合（最新→最老），按整回合累积预算；至少保留最新一个回合。
        used = 0
        keep_from = len(turns)
        for i in range(len(turns) - 1, -1, -1):
            cost = sum(estimate_tokens(m["content"]) for m in turns[i])
            if used + cost <= budget or i == len(turns) - 1:
                used += cost
                keep_from = i
            else:
                break
        recent = [{"role": m["role"], "content": m["content"]} for turn in turns[keep_from:] for m in turn]
        early = [m for turn in turns[:keep_from] for m in turn]
        summary = old_summary
        new_marker = old_marker
        # 读到的都是 marker 之后的未压缩消息，early 全部是新增待压缩的
        if early:
            logger.info("[memory] 历史超预算，增量压缩 %d 条早期消息（marker=%d）", len(early), old_marker)
            compressed_text = "\n\n".join(
                f"[{'用户' if x['role'] == 'user' else '助手'}] {x['content']}"
                for x in early
            )
            await self.tool_result_store.append_history(conv_id, user_id, compressed_text)
            summary = await self._update_summary(old_summary, compressed_text)
            new_marker = early[-1]["id"]
        # 末尾一条是本轮刚落库的用户消息，run_stream 会再追加一次，这里剔除避免重复
        if recent and recent[-1]["role"] == "user":
            recent = recent[:-1]
        # 写回：summary 1 条 + 最近几轮对话原文存短期记忆表，压缩进度 marker 存 conversations 表
        records = []
        if summary:
            records.append({"role": "summary", "content": summary})
        records.extend(recent)
        await self.short_term_memory.replace_records(conv_id, records)
        await self.conversation_store.set_compressed_through(conv_id, new_marker)
        return recent, summary

    async def extract_facts(self, user_id, user_message):
        """异步提炼长期记忆：只从本轮 user 发言提取跨会话可复用偏好，并做语义去重。

        按 user 串行（per-user 锁），只提炼本轮这一条 user 消息；反复出现的偏好靠去重时 importance 提升来强化。
        """
        lock = self._extract_locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            try:
                logger.info("[memory] 开始提炼长期记忆 user=%s", user_id)
                resp = await self.llm.complete([
                    {"role": "system",
                     "content": "从这条用户发言里，只提取【稳定的、跨会话可复用的】偏好或事实（如常住城市、长期的兴趣偏好、习惯性要求，例：'偏好经济型酒店'、'喜欢自然风光'）。不要提取一次性的行程请求参数（如'去成都玩2天预算2000'这种某次旅行的具体目的地/天数/预算），也不要提取助手推荐的内容。没有稳定偏好就输出空列表。每条 fact 不超过 100 个字。每条打 importance(0~1)。输出 JSON:{\"facts\":[{\"fact\":\"...\",\"importance\":0.8}]}"},
                    {"role": "user", "content": user_message}])
                data = json.loads(resp[resp.index("{"):resp.rindex("}") + 1])
                new_facts = data.get("facts", [])
                if not new_facts:
                    return
                existing = await self.long_term_memory.get_all(user_id)
                to_add = []
                for nf in new_facts:
                    fact_text = nf.get("fact", "")
                    importance = nf.get("importance", 0.5)
                    # 语义去重：与已有 fact 相似则更新其 importance（取较大值），否则标记为新增
                    matched = next((ex for ex in existing if _similar(fact_text, ex["fact"])), None)
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

    async def _update_summary(self, old_summary, compressed_text):
        """LLM 把「旧摘要 + 更早对话」浓缩成新的累积摘要；失败回退字符串拼接。

        完整传入 compressed_text、不做截断，避免关键数据（硬数据）被静默丢弃。
        """
        try:
            return await self.llm.complete([
                {"role": "system",
                 "content": "把下面的「旧摘要 + 更早对话」浓缩成一个新的累积摘要（100-300 字），保留核心结论、关键数据（菜名/价格/时间/结构要点）。"},
                {"role": "user",
                 "content": f"旧摘要：{old_summary or '（无）'}\n\n更早对话：\n{compressed_text}"},
            ])
        except Exception:
            logger.warning("[memory] 摘要浓缩失败，回退拼接")
            return (old_summary or "") + "\n" + compressed_text
