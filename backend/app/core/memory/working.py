# app/core/memory/working.py
# 工作记忆：上下文窗口的 token 预算淘汰 + LLM 摘要兜底。
# 可变窗口（历史对话 + ReAct 工具交互）超预算时，成对淘汰最老的 assistant+tool 交互单元，
# 被淘汰单元批量 LLM 蒸馏成摘要放回，保留语义、不裸删。固定头（system/user）由调用方单独维护。
import json
import logging

logger = logging.getLogger(__name__)


class WorkingMemory:
    """工作记忆：token 预算淘汰器（规则化淘汰 + LLM 摘要兜底，对齐 Claude Code 的 layered defense）。

    属性：
        llm:            LLM 客户端（实现 complete 方法，用于蒸馏被淘汰单元）
        budget_tokens:  token 预算上限，超限触发淘汰 + 蒸馏
    """

    def __init__(self, llm, budget_tokens=32000):
        self.llm = llm
        self.budget_tokens = budget_tokens

    def _estimate(self, messages):
        """估算消息列表的 token 数：中文按约 1 字符/token、英文按约 4 字符/token 统计。

        中文（CJK 汉字）tokenizer 的压缩率远低于英文（1 个汉字≈1 个 token），
        若统一按「4 字符/token」会严重低估中文、导致淘汰触发过晚、预算控制失效，故区分中英分别估算。
        """
        total = 0
        for m in messages:
            s = json.dumps(m, ensure_ascii=False)
            cjk = sum(1 for ch in s if '一' <= ch <= '鿿')
            total += cjk + (len(s) - cjk) // 4
        return total

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

    @staticmethod
    def _units(window):
        """把 window 扫描成「单元」列表：assistant(tool_calls) 与其后连续的 tool 消息算一个单元，
        其余消息（纯文本 user/assistant、孤立 tool）各自独立成一个单元。

        淘汰以单元为粒度，保证不会把 assistant 与其 tool 结果拆散（否则 tool 消息失去配对）。
        """
        units = []
        i = 0
        while i < len(window):
            m = window[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                unit = [m]
                i += 1
                # 吞并后续连续的 tool 消息，组成一个完整的交互单元
                while i < len(window) and window[i].get("role") == "tool":
                    unit.append(window[i])
                    i += 1
                units.append(unit)
            else:
                units.append([m])
                i += 1
        return units

    @staticmethod
    def _flatten(units):
        """把单元列表拍平回消息列表。"""
        return [m for unit in units for m in unit]

    async def _distill(self, messages):
        """把一批被淘汰的消息 LLM 蒸馏成一段摘要，保留关键工具、参数与结果要点。"""
        serialized = [self._serialize_message(m) for m in messages]
        return await self.llm.complete([
                                           {"role": "system",
                                            "content": "把下面这段已查过的工具调用与结果蒸馏成一段摘要，保留关键工具、参数与结果要点。"
                                                       "车次号/航班号/票价/时间/日期/地点名等硬数据必须保留精确原值，"
                                                       "不要概括成「有航班」「价格中等」这类模糊描述。"}
                                       ] + serialized)

    async def fit(self, window):
        """超预算时，成对淘汰最老的交互单元，被淘汰单元批量 LLM 蒸馏成摘要放回，直到降到预算内。

        参数 window 是「可变窗口」（历史对话 + ReAct 工具交互），不含 system/user 固定头，
        故可放心从最老开始淘汰。返回处理后的新窗口（不改动入参列表）。
        """
        current = self._estimate(window)
        # 未超预算，原样返回
        if current <= self.budget_tokens:
            return window
        logger.info("[记忆:working] 工作记忆超预算触发淘汰：约 %d token > 预算 %d（共 %d 条消息）",
                    current, self.budget_tokens, len(window))
        units = self._units(window)
        removed = []
        kept = units
        # 从最老（最前）单元开始淘汰，直到估算降到预算内；至少保留一个单元避免窗口被清空
        while self._estimate(self._flatten(kept)) > self.budget_tokens and len(kept) > 1:
            removed.extend(kept.pop(0))
        # 只剩一个单元仍超预算（单条极大），无法再淘汰，原样返回避免死循环
        if not removed:
            return window
        # 被淘汰单元批量 LLM 蒸馏成一段摘要，替代原始交互放回窗口最前（保留语义、不裸删）
        summary = await self._distill(removed)
        result = [{"role": "system", "content": "[早期交互摘要] " + summary}] + self._flatten(kept)
        logger.info("[记忆:working] 淘汰完成：移除 %d 条消息蒸馏成摘要，窗口降至约 %d token",
                    len(removed), self._estimate(result))
        return result
