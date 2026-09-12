# app/core/critic.py
# LLM critic 回路：事实校验专家。对「硬数据工具」场景下的回答做一次事后校验，
# 找出无法被工具结果支撑、或与工具结果矛盾的事实声明，返回 ok/issues 供上层决定是否修正。
# 子 agent 由 create_agent 构造（与主循环共用同一套 ReAct 实现）：多轮带工具，
# critic 可用 read_file/grep 定向读回完整结果做 grounded 校验（对齐 CRITIC/CoV），
# 限轮数 + fail-open，避免「把完整结果一次性塞进上下文」撞窗口上限。
import logging
import time

from langchain.agents import create_agent
from pydantic import BaseModel

from app.core.prompts import load_prompt, today_hint

logger = logging.getLogger(__name__)


class CriticIssue(BaseModel):
    """一条事实问题：原句 + 问题 + 具体怎么改。"""

    claim: str
    problem: str
    # correction 必须有默认值：判 ok=true 时模型没有 correction 可填，
    # 设成必填会逼模型编一条出来，而空 correction 的 issue 会被当作误报过滤掉。
    correction: str = ""


class CriticVerdict(BaseModel):
    """critic 判定结果：ok=false 时 issues 列出必须修正的事实问题。"""

    ok: bool
    issues: list[CriticIssue] = []


def build_critic_agent(model, tools):
    """构造 critic 子 agent：多轮带工具 + 结构化输出，与主循环共用 create_agent 实现。"""
    return create_agent(model, tools=tools, system_prompt=load_prompt("critic"),
                        response_format=CriticVerdict)


async def run_critic(model, tool_results: str, answer: str, read_file_tool=None, grep_tool=None,
                     max_iter: int = 3) -> dict:
    """对回答做事实校验，返回 {"ok": bool, "issues": [{"claim","problem","correction"}, ...]}。

    参数：
        model:        langchain 模型实例（create_agent 驱动，结构化输出走工具调用）
        tool_results: 本轮硬数据工具的结果（模型所见语料，序列化文本），作为校验的事实依据
        answer:       待校验的回答文本
        read_file_tool: 可选，read_file 工具（langchain tool 对象），供 critic 定向读回完整结果
        grep_tool:      可选，grep 工具（langchain tool 对象）；两者为 None 时退化为单次调用（不附工具）
        max_iter:     带工具校验的最大轮数（含最终结论轮），超限 fail-open
    返回：
        dict：{"ok": 是否所有事实可被工具结果支撑, "issues": 有问题的声明列表}；
        调用异常 / 超限 / 结论缺失时兜底 {"ok": True, "issues": []}（宁可放过，不阻塞主流程）。
    """
    start = time.perf_counter()
    logger.info("[critic] 开始事实校验：工具结果 %d 字、回答 %d 字", len(tool_results), len(answer))
    # 事实依据（工具结果）放前、待校验回答放后，让 critic 逐条比对；
    # 注入今天日期，避免 critic 用训练数据里的旧年份误判「当前年份」的数据为「未来/非当前」
    user = f"{today_hint()}\n工具结果：\n{tool_results}\n\n待校验回答：\n{answer}"
    # 多轮带工具：critic 可调用 read_file/grep 读回完整结果；无工具时退化为单次调用。
    # system 提示词由 build_critic_agent 的 system_prompt 注入，这里只给本轮语料
    tools = [read_file_tool, grep_tool] if read_file_tool is not None else []
    try:
        agent = build_critic_agent(model, tools)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": user}]},
            # create_agent 每轮「模型 + 工具」各占一个超步，N 轮需 2N-1：第 N 轮模型调用仍放行，
            # 它若还在调工具，下一超步即触顶抛 GraphRecursionError
            config={"recursion_limit": 2 * max_iter - 1},
        )
    except Exception as e:
        # LLM 调用失败（配额耗尽 403 / 网络抖动等）/ 超轮数 / 其他异常：
        # 跳过校验，不因校验器故障中断 SSE 主流程
        logger.warning("[critic] 校验调用失败，跳过事实校验：%s", e)
        return {"ok": True, "issues": []}

    elapsed = time.perf_counter() - start
    # 未拿到 verdict（模型没调结构化输出工具）说明模型没按契约下结论：宁可放过
    data = result.get("structured_response")
    if not isinstance(data, CriticVerdict):
        logger.info("[critic] 校验完成，耗时 %.1fs，判定结果解析失败，按通过处理", elapsed)
        return {"ok": True, "issues": []}
    raw_issues = [i.model_dump() for i in data.issues]
    # 过滤「correction 为空」的 issue：critic 自己都填不出「怎么改」的，大概率是误报
    # （把一致的声明/格式差异硬凑成问题），不能据此触发修正，否则会把正确答案改坏。
    valid_issues = [i for i in raw_issues
                    if isinstance(i, dict) and str(i.get("correction", "")).strip()]
    if raw_issues and not valid_issues:
        result = {"ok": True, "issues": []}
    else:
        result = {"ok": bool(data.ok), "issues": valid_issues}
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
