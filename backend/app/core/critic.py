# app/core/critic.py
# LLM critic 回路：事实校验专家。对「硬数据工具」场景下的回答做一次事后校验，
# 找出无法被工具结果支撑、或与工具结果矛盾的事实声明，返回 ok/issues 供上层决定是否修正。
import json
import logging
import time

from app.core.prompts import load_prompt

logger = logging.getLogger(__name__)


async def run_critic(llm, tool_results: str, answer: str) -> dict:
    """对回答做事实校验，返回 {"ok": bool, "issues": [{"claim","problem","correction"}, ...]}。

    参数：
        llm:           LLM 客户端（实现 LLMClient 接口，用 chat + response_format 强制 JSON）
        tool_results:  本轮硬数据工具的完整结果（序列化文本），作为校验的事实依据
        answer:        待校验的回答文本
    返回：
        dict：{"ok": 是否所有事实可被工具结果支撑, "issues": 有问题的声明列表}；
        解析失败时兜底 {"ok": True, "issues": []}（宁可放过，不因校验器故障阻塞主流程）。
    """
    system = load_prompt("critic")
    # 事实依据（工具结果）放前、待校验回答放后，让 critic 逐条比对
    user = f"工具结果：\n{tool_results}\n\n待校验回答：\n{answer}"
    start = time.perf_counter()
    logger.info("[critic] 开始事实校验：工具结果 %d 字、回答 %d 字", len(tool_results), len(answer))
    try:
        resp = await llm.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            response_format={"type": "json_object"})
    except Exception as e:
        # LLM 调用失败（配额耗尽 403 / 网络抖动等）时跳过校验，不因校验器故障中断 SSE 主流程
        logger.warning("[critic] 校验调用失败，跳过事实校验：%s", e)
        return {"ok": True, "issues": []}
    data = _parse_json(resp.get("content", ""))
    elapsed = time.perf_counter() - start
    if not isinstance(data, dict):
        # 判定结果解析失败：宁可放过，不因校验器故障阻塞主流程
        logger.info("[critic] 校验完成，耗时 %.1fs，判定结果解析失败，按通过处理", elapsed)
        return {"ok": True, "issues": []}
    result = {"ok": bool(data.get("ok", True)), "issues": data.get("issues", [])}
    logger.info("[critic] 校验完成，耗时 %.1fs，ok=%s，issue %d 条",
                elapsed, result["ok"], len(result["issues"]))
    return result


def _parse_json(text):
    """从 LLM 输出中提取首个完整 JSON 对象（忽略尾随文本），失败返回 None。"""
    start = text.find("{")
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
        return obj
    except json.JSONDecodeError:
        return None
