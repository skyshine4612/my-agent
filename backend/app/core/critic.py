# app/core/critic.py
# LLM critic 回路：事实校验专家。对「硬数据工具」场景下的回答做一次事后校验，
# 找出无法被工具结果支撑、或与工具结果矛盾的事实声明，返回 ok/issues 供上层决定是否修正。
# 多轮带工具：critic 可用 read_file/grep 定向读回完整结果做 grounded 校验（对齐 CRITIC/CoV），
# 限轮数 + fail-open，避免「把完整结果一次性塞进上下文」撞窗口上限。
import json
import logging
import time

from app.core.prompts import load_prompt, today_hint
from app.tools.file import READ_FILE_TOOL, GREP_TOOL

logger = logging.getLogger(__name__)


async def run_critic(llm, tool_results: str, answer: str, read_file_fn=None, grep_fn=None, max_iter: int = 3) -> dict:
    """对回答做事实校验，返回 {"ok": bool, "issues": [{"claim","problem","correction"}, ...]}。

    参数：
        llm:          LLM 客户端（实现 LLMClient 接口，用 chat + response_format 强制 JSON）
        tool_results: 本轮硬数据工具的结果（模型所见语料，序列化文本），作为校验的事实依据
        answer:       待校验的回答文本
        read_file_fn: 可选，异步函数 (path, offset, limit) -> dict | None，供 critic 用 read_file 读结果文件
        grep_fn:      可选，异步函数 (pattern, path) -> dict，供 critic 用 grep 搜结果文件；
                      两者为 None 时退化为单次调用（不附工具）
        max_iter:     带工具校验的最大轮数（含最终结论轮），超限 fail-open
    返回：
        dict：{"ok": 是否所有事实可被工具结果支撑, "issues": 有问题的声明列表}；
        解析失败 / 调用异常 / 超限时兜底 {"ok": True, "issues": []}（宁可放过，不阻塞主流程）。
    """
    system = load_prompt("critic")
    # 事实依据（工具结果）放前、待校验回答放后，让 critic 逐条比对；
    # 注入今天日期，避免 critic 用训练数据里的旧年份误判「当前年份」的数据为「未来/非当前」
    user = f"{today_hint()}\n工具结果：\n{tool_results}\n\n待校验回答：\n{answer}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    start = time.perf_counter()
    logger.info("[critic] 开始事实校验：工具结果 %d 字、回答 %d 字", len(tool_results), len(answer))
    # 多轮带工具：critic 可调用 read_file/grep 读回完整结果；无 read_file_fn 时不附工具（单次退化）
    tools = [READ_FILE_TOOL, GREP_TOOL] if read_file_fn is not None else None
    data = None
    for i in range(max_iter):
        try:
            resp = await llm.chat(messages, tools=tools, response_format={"type": "json_object"})
        except Exception as e:
            # LLM 调用失败（配额耗尽 403 / 网络抖动等）时跳过校验，不因校验器故障中断 SSE 主流程
            logger.warning("[critic] 校验调用失败，跳过事实校验：%s", e)
            return {"ok": True, "issues": []}
        calls = resp.get("tool_calls")
        if calls and read_file_fn is not None:
            logger.info("[critic] 第 %d 轮决定调用 %d 个工具：%s", i + 1, len(calls),
                        ", ".join(tc.get("function", {}).get("name", "") for tc in calls))
            # 回填 assistant 的 tool_calls 与每个 read_file/grep 的结果，让 critic 继续读/判
            messages.append({"role": "assistant", "content": "", "tool_calls": calls})
            for tc in calls:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                logger.info("[critic] 第 %d 轮 调工具 %s，参数 %s", i + 1, fn["name"], json.dumps(args, ensure_ascii=False))
                if fn["name"] == "read_file":
                    seg = await read_file_fn(args.get("path"), args.get("offset", 0), args.get("limit", 3500))
                else:  # grep
                    seg = await grep_fn(args.get("pattern", ""), args.get("path"))
                content = json.dumps(seg, ensure_ascii=False) if seg is not None else "未找到该文件"
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": content})
            continue
        # 无 tool_calls：本轮是最终结论，解析 JSON 后跳出
        logger.info("[critic] 第 %d 轮下结论", i + 1)
        data = _parse_json(resp.get("content", ""))
        break
    else:
        # 达到 max_iter 仍未下结论（每轮都在读）：fail-open，不阻塞主流程
        logger.info("[critic] 达到 %d 轮上限未收敛，按通过处理", max_iter)
        return {"ok": True, "issues": []}

    elapsed = time.perf_counter() - start
    if not isinstance(data, dict):
        # 判定结果解析失败：宁可放过，不因校验器故障阻塞主流程
        logger.info("[critic] 校验完成，耗时 %.1fs，判定结果解析失败，按通过处理", elapsed)
        return {"ok": True, "issues": []}
    result = {"ok": bool(data.get("ok", True)), "issues": data.get("issues", [])}
    # 过滤「correction 为空」的 issue：critic 自己都填不出「怎么改」的，大概率是误报
    # （把一致的声明/格式差异硬凑成问题），不能据此触发修正，否则会把正确答案改坏。
    raw_issues = result.get("issues", [])
    valid_issues = [i for i in raw_issues
                    if isinstance(i, dict) and str(i.get("correction", "")).strip()]
    if raw_issues and not valid_issues:
        result = {"ok": True, "issues": []}
    else:
        result = {"ok": bool(data.get("ok", True)), "issues": valid_issues}
    logger.info("[critic] 校验完成，耗时 %.1fs，ok=%s，issue %d 条（过滤无效 %d 条）",
                elapsed, result["ok"], len(result["issues"]), len(raw_issues) - len(valid_issues))
    if not result["ok"]:
        # 逐条打印问题详情，便于排查为什么判定不通过
        for i, issue in enumerate(result["issues"][:10], 1):
            logger.info("[critic]   issue #%d: claim=%s | problem=%s | correction=%s",
                        i,
                        str(issue.get("claim", ""))[:100],
                        str(issue.get("problem", ""))[:60],
                        str(issue.get("correction", ""))[:60])
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
