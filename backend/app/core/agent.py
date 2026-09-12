# app/core/agent.py
# Agent 基类：ReAct 推理-行动循环（LLM 决策 → 工具调用 → 回填结果 → 再决策）由 create_agent 编译出的
# 一张图承载，不套父图；工具执行（并行 + 结构化截断 + 地址索引落盘）、工作记忆 token 预算淘汰、
# critic 事实校验与定向改写各落在一个中间件上，后者的环跑在收口钩子内部，对图不可见。
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (AIMessage, HumanMessage, RemoveMessage,
                                     SystemMessage, ToolMessage)
from langchain_core.messages.utils import convert_to_openai_messages
from langgraph.errors import GraphRecursionError
from langgraph.graph.message import add_messages
from langgraph.types import Command

from app.config import settings
from app.core.context import current_user_id
from app.core.critic import run_critic
from app.core.memory.working import WorkingMemory

logger = logging.getLogger(__name__)

# 非查询结果工具：返回「读回片段/规则正文/委派摘要」而非「查询结果」。
# 它们的返回原样回填窗口（不结构化截断），也不触发地址索引（不写文件）——
# 否则 read_file 读回的片段又被截断/写文件，会形成 read→截断/spill→read 的循环。
NON_QUERY_TOOLS = {"read_file", "grep", "get_skill", "call_sub_agent"}

# 硬数据工具集合：这些工具返回的是「事实性」数据（车次/航班/天气/节假日），最容易被 LLM 编造或扭曲，
# 只有本轮调用了它们才触发 critic 回路（省成本，仅在硬数据场景做事实校验）。
HARD_DATA_TOOLS = {"train_ticket_query", "flight_query", "weather_query", "holiday_calendar"}

# 图侧步数额度：一轮 ReAct 占 3 步（before_model 钩子 + 模型 + 工具），60 步 = 20 轮。
# 防死循环的粗上界，不是精确契约。
RECURSION_LIMIT = 60

# 配置里按值携带凭据的字段：工具异常常带完整请求 URL（高德把 key 放 query）、连接串异常带密码。
# 两个 MCP 服务地址不含凭据（token 走 header），故不在列。
_SECRET_FIELDS = ("llm_api_key", "embedding_api_key", "vision_api_key", "variflight_api_key",
                  "uapis_api_key", "tavily_api_key", "amap_api_key", "modelscope_token",
                  "supabase_jwt_secret", "supabase_database_url", "supabase_test_database_url")


def _redact_secrets(text):
    """按值抹掉文本里已配置的凭据（未配置的字段与空值跳过）。"""
    for name in _SECRET_FIELDS:
        value = getattr(settings, name, "")
        if value:
            text = text.replace(value, "***")
    return text


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


async def postprocess_tool_result(name, raw, tool_result_store, conversation_id, user_id):
    """工具结果后处理：结构化截断 + 超限 spill，返回喂给模型的文本。

    这份文本就是模型所见语料，critic 复用同一份，故截断维度与文案必须一字不改。
    抽成独立函数只为便于单测 —— middleware 只负责调它。
    """
    if name in NON_QUERY_TOOLS:
        # 非查询结果工具（read_file/grep/get_skill/call_sub_agent）：返回内容原样回填，
        # 不做结构化截断（读回片段/规则正文本身就是有界且需要完整的），也不触发地址索引
        return str(raw)
    text, note = _struct_truncate_to_str(raw, 20, 20, settings.truncate_limit)
    # 地址索引：完整结果被截断（list 超20 / dict 超20键 / str 超阈值）时写临时文件，
    # 窗口只放预览 + 文件路径，模型可 read_file/grep 读回被省略部分
    if (tool_result_store is not None and conversation_id is not None
            and _needs_spill(raw, list_limit=20, str_limit=settings.truncate_limit)):
        path = await tool_result_store.write(conversation_id, user_id, str(raw))
        parts = [note, f"完整结果存到 {path}"] if note else [f"完整结果存到 {path}"]
        return f"{text}\n[{'; '.join(parts)}，可用 read_file/grep 读回剩余]"
    if note:
        return f"{text}[{note}]"
    return text


def _concat(a, b):
    """自定义键的累积 reducer：本轮内只追加（None 视为空）。"""
    return (a or []) + (b or [])


class TurnState(TypedDict):
    """一轮对话的图 state：messages 是 ReAct 语料，其余键是本轮跨阶段累积的事实。

    自定义键一律带累积 reducer、且不由 messages 反推：工作记忆淘汰会从 state 里删掉早先的
    assistant 消息，反推会丢掉轮次与工具结果，而 critic 校验、定向改写、短期记忆落库都要用它们。
    """
    messages: Annotated[list, add_messages]
    hard_results: Annotated[list, _concat]   # 硬数据工具结果（模型所见语料），critic 的触发判据与校验依据
    tool_results: Annotated[list, _concat]   # 本轮每个工具 {tool, args, result}，定向改写的事实依据
    rounds: Annotated[list, _concat]         # 每轮 tool_calls（ReAct 轮次），供短期记忆按轮次落库
    answer: str                              # 面向用户的最终答案（critic 改写后覆盖 React 轮的答案）


@dataclass
class CriticConfig:
    """一轮对话的 critic 回路装配；不传即不做事实校验（子 Agent 就是这种）。

    read_file_tool / grep_tool：critic 定向读回完整结果用的工具（None 则退化为单次调用）；
    skills：get_skill 加载过的业务规则正文，定向改写时注回（保住输出格式约束）；
    max_iter：critic 单次判定的最大轮数；max_rewrite：判不通过后最多改写几次（故 critic 最多跑 max_rewrite + 1 轮）。
    """
    read_file_tool: object = None
    grep_tool: object = None
    skills: list = field(default_factory=list)
    max_iter: int = 3
    max_rewrite: int = 2


def _react_answer(state):
    """React 轮的答案：messages 里末条不带 tool_calls 的 assistant 消息内容。

    未收敛触顶时返回空串，但那种情形下收口钩子根本跑不到，故不存在把兜底文案冲成空答案的风险。
    """
    for m in reversed(state.get("messages") or []):
        if isinstance(m, AIMessage) and not m.tool_calls:
            return m.content if isinstance(m.content, str) else ""
    return ""


def _loaded_skill_names(tool_results):
    """本轮 get_skill 加载过的业务名（从事实依据里取，与生成轮加载的一致）。"""
    return {t["args"].get("name") for t in tool_results
            if t.get("tool") == "get_skill" and isinstance(t.get("args"), dict)}


class _CriticMiddleware(AgentMiddleware):
    """critic 回路中间件：ReAct 循环收口（after_agent）时做事实校验，不通过则复用本轮已查到的
    工具结果定向改写，改写后回头再校验，直到通过或达 max_rewrite 上限。

    环对本钩子之外完全不可见：校验与改写都是普通 async 调用，不新增图节点、不占 recursion_limit。
    触发条件与图外时代一致：本轮真的调用了硬数据工具（hard_results 由 _ToolExecMiddleware 累积）。

    不包 try/except：run_critic 内部已吞掉三条 fail-open（超限 / 结论解析失败 / LLM 调用失败），
    而改写那次 model.ainvoke 的异常在旧代码里就是向上抛给 worker 的，兜底会静默改变行为。
    """

    def __init__(self, model, config: CriticConfig, system_prompt, user_message, summary_hint, emit):
        super().__init__()
        self.model = model
        self.config = config
        self.system_prompt = system_prompt
        self.user_message = user_message
        self.summary_hint = summary_hint
        self.emit = emit

    async def aafter_agent(self, state, runtime):
        if not state.get("hard_results"):
            return None
        await self.emit({"type": "status", "status": "checking"})
        answer = _react_answer(state)
        for attempt in range(self.config.max_rewrite + 1):
            verdict = await run_critic(
                self.model, json.dumps(state.get("hard_results") or [], ensure_ascii=False),
                answer, read_file_tool=self.config.read_file_tool,
                grep_tool=self.config.grep_tool, max_iter=self.config.max_iter)
            # 末轮判定不再改写：改完没有机会复检，等于把未校验的答案直接发给用户
            if verdict.get("ok", True) or attempt == self.config.max_rewrite:
                break
            answer = await self._rewrite(state, verdict.get("issues") or [])
        return {"answer": answer}

    async def _rewrite(self, state, issues):
        """定向改写：复用本轮已查到的工具结果让模型重写答案——不重新调工具、不走 ReAct。"""
        await self.emit({"type": "status", "status": "correcting"})
        logger.info("[rewrite] critic 判定不通过（%d 条问题），进入修正答案", len(issues))
        tool_results = state.get("tool_results") or []
        # 修正 prompt 只喂 {tool,args,result} 事实依据，result 取模型所见语料（截断版）而非展示用 summary
        tool_results_text = json.dumps(
            [{"tool": t["tool"], "args": t["args"], "result": t.get("result", "")} for t in tool_results],
            ensure_ascii=False)
        # 注入本轮 get_skill 加载过的业务规则正文，保住「输出格式」等约束（修正不重新调 get_skill）
        loaded = _loaded_skill_names(tool_results)
        skill_guide = "\n\n".join(
            f"【{s['name']} 业务规则】\n{s['body']}"
            for s in self.config.skills if s["name"] in loaded
        )
        correction_prompt = (
                f"原始问题：{self.user_message}\n\n"
                f"以下是本轮已经查询到的工具结果（事实依据，请严格基于这些，不要编造）：\n{tool_results_text}\n\n"
                + (
                    f"以下是本业务的规划规则与输出格式，修正后的答案必须同样遵守：\n{skill_guide}\n\n" if skill_guide else "")
                + f"请基于以上工具结果直接输出修正后的最终答案（中文 Markdown）。"
                  f"用户明确询问但工具没查到的关键信息，如实说明「该信息未查到」，不要编造；用户没问的字段不要罗列缺失。\n"
                  f"修正要点：\n{json.dumps(issues, ensure_ascii=False)}"
        )
        msgs = [{"role": "system", "content": self.system_prompt}]
        if self.summary_hint:
            msgs.append({"role": "system", "content": self.summary_hint})
        msgs.append({"role": "user", "content": correction_prompt})
        t0 = time.perf_counter()
        resp = await self.model.ainvoke(msgs)
        answer = resp.content if isinstance(resp.content, str) else ""
        logger.info("[rewrite] 修正答案完成，耗时 %.1fs，%d 字", time.perf_counter() - t0, len(answer))
        return answer


class _ToolExecMiddleware(AgentMiddleware):
    """工具执行中间件：完全短路框架的工具执行路径，自己执行工具并回填 tool 消息 + 本轮事实。

    为什么完全不调 handler：handler 就是真实执行路径（ToolNode → tool.ainvoke），
    「先 handler 再自己执行」会让工具被执行两次 —— 对 train_ticket_query 这类外部配额工具是双倍消耗。
    代价是绕过 ToolNode 的注入参数/内层错误处理/响应格式化，故这里自行复刻：
    错误回填（异常转文本回喂 LLM，而非炸掉整轮）与 ToolMessage 自建。
    """

    def __init__(self, agent_name, registry, emit, conversation_id=None,
                 tool_result_store=None, user_id=None):
        super().__init__()
        self.agent_name = agent_name
        self.registry = registry
        self.emit = emit
        self.conversation_id = conversation_id
        self.tool_result_store = tool_result_store
        self.user_id = user_id
        # 已记进 rounds 的 assistant 消息 id：同一轮多个工具并行时，每个调用各过一遍本钩子
        self._round_ids = set()

    def _round_tool_calls(self, state):
        """本轮 assistant 消息的 tool_calls（OpenAI 形状），同一轮只返回一次。"""
        ai = next((m for m in reversed(state["messages"])
                   if isinstance(m, AIMessage) and m.tool_calls), None)
        if ai is None or ai.id in self._round_ids:
            return None
        self._round_ids.add(ai.id)
        return convert_to_openai_messages([ai])[0]["tool_calls"]

    async def awrap_tool_call(self, request, handler):
        # 工具未注册时 request.tool 为 None，退回框架路径；这条路径只会执行一次，不会双跑
        if request.tool is None:
            return await handler(request)
        tc = request.tool_call
        name = tc["name"]
        # 框架已把 arguments 解析成 dict，直接取用；缺省时空 dict 即无参调用
        args = tc.get("args") or {}
        # 轮次只用于日志：state 里已产生 tool_calls 的 assistant 消息数即当前轮
        round_no = max(1, sum(1 for m in request.state["messages"]
                              if isinstance(m, AIMessage) and m.tool_calls))
        logger.info("[Agent:%s] 第 %d 轮 调工具 %s（id=%s），参数 %s", self.agent_name, round_no, name, tc["id"],
                    json.dumps(args, ensure_ascii=False))
        await self.emit({"type": "tool_call", "agent": self.agent_name, "tool": name, "args": args,
                         "id": tc["id"], "label": self.registry.label(name)})
        # 通过 registry 真正执行工具，拿到结构化原始结果（call_raw 不做 str() 转换）；
        # 失败时把错误回传给 LLM 而非让整轮崩溃（MCP 限流/网络抖动会抛异常）
        try:
            raw = await self.registry.call_raw(name, args)
        except Exception as e:
            # 出口共用于所有数据源：同一个异常串会进日志、回填模型、下发前端并落库，四处一起脱敏
            err = _redact_secrets(str(e))
            logger.warning("[Agent:%s] 工具 %s 执行失败：%s", self.agent_name, name, err)
            raw = f"工具执行失败：{err}"
        # 后处理与上报的异常会被框架的工具错误兜底吞成 error ToolMessage（降级不炸轮），
        # 不补日志就完全无痕：这里记完再原样抛出，保持框架既有的降级行为与错误串
        try:
            text = await postprocess_tool_result(name, raw, self.tool_result_store,
                                                 self.conversation_id, self.user_id)
            logger.info("[Agent:%s] 第 %d 轮 工具 %s 完成（id=%s），返回 %.120s",
                        self.agent_name, round_no, name, tc["id"], text)
            # result 字段是模型工作记忆里的截断版结果，供上层 critic 校验，保证校验语料与模型所见一致
            await self.emit({"type": "tool_result", "agent": self.agent_name, "tool": name,
                             "status": True, "result": text, "id": tc["id"],
                             "label": self.registry.label(name)})
        except Exception:
            logger.exception("[Agent:%s] 第 %d 轮 工具 %s（id=%s）结果后处理/上报失败，交由框架降级为错误回填",
                             self.agent_name, round_no, name, tc["id"])
            raise
        # 不回填 name：回填消息的 token 估算（WorkingMemory._estimate 序列化整条消息）必须只含
        # role / tool_call_id / content 三个键，多一个键就会抬高估算、把工作记忆的淘汰边界整体前移
        update = {"messages": [ToolMessage(content=text, tool_call_id=tc["id"])],
                  # result 取模型所见语料（截断版），供 critic 校验与修正重写复用，保证语料一致
                  "tool_results": [{"tool": name, "args": args, "result": text}]}
        if name in HARD_DATA_TOOLS:
            update["hard_results"] = [text]
        round_calls = self._round_tool_calls(request.state)
        if round_calls:
            update["rounds"] = [round_calls]
        return Command(update=update)


class _WorkingMemoryMiddleware(AgentMiddleware):
    """工作记忆中间件：每轮模型调用前按 token 预算淘汰最老的工具交互（超预算时 LLM 蒸馏成摘要兜底），
    并把「模型所见窗口」复位成「固定头 → 淘汰摘要 → 保留窗口 → 本轮用户指令」。

    淘汰只裁剪「本轮可变窗口」——最后一条 user 消息之后的消息，即本轮的工具交互；
    固定头（system prompt / 历史摘要）与短期记忆（历史原文）不参与淘汰。

    布局复位只在「模型所见」这一层做，不动 state：state 只追加（用户指令停在原位、摘要只能追加到
    末尾），若为了凑顺序去改 state，rounds / 落库 / 淘汰单元切分都会跟着漂。
    """

    def __init__(self, wm=None):
        super().__init__()
        self.wm = wm
        # 蒸馏摘要不进 state：它由本 middleware 持有，只用于拼接模型所见窗口，
        # 这样下一轮喂给 fit 的窗口就是 [摘要] + 保留单元，淘汰单元与计数器口径不变
        self.summary = None

    async def abefore_model(self, state, runtime):
        if self.wm is None:
            return None
        messages = state["messages"]
        # 窗口起点 = 最后一条 user 消息（本轮用户指令）：其后才是本轮工具交互
        start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        window = list(messages[start + 1:])
        # WorkingMemory 的既有实现处理 OpenAI 形状的 dict，故先转形状；
        # 上轮淘汰留下的摘要置于窗口最前，fit 才能在超预算时先淘汰它
        converted = convert_to_openai_messages(window)
        if self.summary:
            converted = [{"role": "system", "content": self.summary}] + converted
        fitted = await self.wm.fit(converted)
        # 未超预算时 fit 原样返回入参本身，此时窗口与摘要都没变
        if fitted is converted:
            return None
        # 超预算：fit 返回 [新摘要] + 保留单元；被淘汰的前缀（从最老单元开始）从 state 里删除，
        # 保留单元原地不动，摘要转为 middleware 自己的状态（见 __init__）
        summary = fitted[0] if fitted and fitted[0].get("role") == "system" else None
        if summary:
            self.summary = summary["content"]
        kept = len(fitted) - (1 if summary else 0)
        evicted = window[:len(window) - kept]
        if not evicted:
            return None
        return {"messages": [RemoveMessage(id=m.id) for m in evicted]}

    async def awrap_model_call(self, request, handler):
        # 模型所见 = 框架的 system prompt（request.system_message）+ request.messages，这里只重排后者
        messages = list(request.messages)
        humans = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
        if not humans:
            return await handler(request)
        last_user = humans[-1]
        # 模型所见布局：固定头与历史 → 淘汰摘要 → 保留窗口 → 本轮用户指令（末尾）
        ordered = (messages[:last_user]
                   + ([SystemMessage(content=self.summary)] if self.summary else [])
                   + messages[last_user + 1:]
                   + [messages[last_user]])
        return await handler(request.override(messages=ordered))


@dataclass
class Agent:
    """Agent 基类：把 LLM 推理与工具执行编排为 ReAct「思考-行动-观察」循环。

    属性：
        name:            Agent 名称（用于日志与追踪）
        system_prompt:   系统提示词，注入每轮对话的最前
        tools:           本 Agent 可调用的工具名列表
        model:           langchain 模型实例（create_agent 驱动主循环，工作记忆蒸馏也走它）
    """
    name: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    model: object = None

    async def run_stream(self, user_message, history, registry, on_event=None,
                         conversation_id=None, context_budget=None,
                         tool_result_store=None, summary=None, critic=None):
        """以流式方式执行一轮对话，返回 (最终累积文本答复, 每轮 tool_calls)。

        流程：
            - ReAct 循环由 create_agent 编译出的一张图承载：模型节点做决策，工具节点被 _ToolExecMiddleware 完全短路；
            - 图在模型不再调工具处收口，_CriticMiddleware 在收口钩子里做事实校验与定向改写（环不占图额度）；
            - 调工具轮次的 content 是内部思考（thought），不上发前端；
            - 仅最终答案（不调工具的轮次，含 critic 改写后的答案）经 on_event 发为 token 事件；
            - 同一轮多个 tool_calls 由框架并行执行，tool 消息按原始顺序回填；
            - 工具结果回填前做结构化截断（list 前20 / dict 前20键 / str truncate_limit），
              tool_result 事件的 result 字段是模型工作记忆里的截断版结果，供 critic 校验，
              保证 critic 校验所用语料与模型生成答案时所见一致；
            - 每轮模型调用前，_WorkingMemoryMiddleware 按 token 预算淘汰最老交互（超预算时 LLM 蒸馏成摘要兜底）。

        conversation_id：  工具结果地址索引的隔离维度（主会话或子会话 id）。
        context_budget：   工作记忆 token 预算，为 None 时不淘汰（保持原有无限累积行为）。
        tool_result_store：工具结果地址索引存储（ToolResultStore），为 None 时不落库（不启用地址索引）。
        summary：          历史对话摘要，非空时作为独立 system 消息紧跟主 system prompt 之后。
        critic：           CriticConfig，非空时挂上 critic 收口中间件（事实校验与定向改写）；为 None 时只跑 ReAct。

        on_event：可选异步回调，接收流式事件（token / tool_call / tool_result / status）；为 None 时静默。
        """

        async def emit(event):
            """向回调上报一个流式事件（无回调则静默跳过）。"""
            if on_event is not None:
                await on_event(event)

        logger.info("[Agent:%s] 开始执行（流式），输入：%.60s", self.name, user_message)
        # 工作记忆（预算淘汰器）：传入 model 与预算时启用，否则禁用（不淘汰）
        wm = WorkingMemory(self.model, context_budget) if context_budget else None
        # 输入消息：主 system 提示词（含 long_term_hint）由 create_agent 的 system_prompt 注入在最前、
        # 永不淘汰；历史摘要作为独立 system 消息紧随其后；之后拼短期记忆（最近几轮对话原文，
        # 持久，不参与淘汰）与当前用户指令
        inputs = []
        if summary:
            inputs.append(SystemMessage(content=summary))
        inputs.extend(history)
        inputs.append(HumanMessage(content=user_message))
        # 工具取注册表里的 langchain tool 对象：create_agent 需要真工具对象，middleware 才拿得到
        # request.tool；工具 schema 由工具自身的 args_schema 生成，与 to_openai_schemas 同源
        tools = [registry.get(n).fn for n in self.tools] if self.tools else []
        # 装配决策 A：本轮 new 一个 middleware 实例（带本轮 conversation_id / user_id），并在本轮内建图 ——
        # conversation_id 每轮不同，挂成可变共享属性会在并发两轮对话时互相踩
        middleware = [_ToolExecMiddleware(self.name, registry, emit,
                                          conversation_id=conversation_id,
                                          tool_result_store=tool_result_store,
                                          user_id=current_user_id.get()),
                      # 即使不淘汰（wm 为 None）也要挂：它同时负责复位「模型所见窗口」的布局
                      _WorkingMemoryMiddleware(wm)]
        if critic is not None:
            # 必须挂在末尾：框架只把「最后一个带 after_agent 的 middleware」当作收口节点
            middleware.append(_CriticMiddleware(self.model, critic, self.system_prompt,
                                                user_message, summary, emit))
        # 图按本轮装配：conversation_id / middleware / critic 都是本轮的，
        # state schema 用 TurnState（messages + 本轮累积的工具结果/轮次/答案）
        graph = create_agent(self.model, tools=tools, system_prompt=self.system_prompt,
                             middleware=middleware, state_schema=TurnState)
        # 累积每轮的 tool_calls（ReAct 轮次）：供上层按轮次落库短期记忆（每轮一条 assistant(tool_calls)）
        rounds = []
        recorded = set()  # 已记录的答案消息 id：同一超步的 state 会在后续超步里重复出现，按 id 去重
        answer = None  # 不调工具轮次的 content = 面向用户的最终答案（None = 尚未产出）
        status_sent = False
        try:
            # 多模式流式：messages 拿模型的实时输出（发 status），values 拿每个超步的完整 state
            # （读最终答案与 middleware 写回 state 的轮次——工作记忆淘汰会把早先的 assistant 消息
            #  从 state 里删掉，故轮次不能从 messages 反推）
            async for mode, payload in graph.astream(
                    {"messages": inputs, "hard_results": [], "tool_results": [], "rounds": []},
                    # 限额按「任务」计，一轮 ReAct 占 3 个：before_model 钩子 + 模型 + 工具。
                    # 模型跑到额度用尽仍未收口时落到兜底文案。
                    config={"recursion_limit": RECURSION_LIMIT},
                    stream_mode=["messages", "values"]):
                if mode == "messages":
                    chunk, meta = payload
                    # LLM 开始流式产出（思考或答案）时发一次 status，让前端显示「整理中」，覆盖生成期的长静默；
                    # 工具消息出现即视为进入下一轮，重置标记（每轮首个内容块只发一次）
                    if isinstance(chunk, ToolMessage):
                        status_sent = False
                    elif isinstance(chunk, AIMessage) and meta.get("langgraph_node") == "model":
                        # 只认模型的产出：critic 校验与改写在收口钩子内部的嵌套图里跑，
                        # 其结论轮不该当成一次「开始生成」多发 status
                        text = chunk.content if isinstance(chunk.content, str) else ""
                        if text and not status_sent:
                            await emit({"type": "status", "status": "generating"})
                            status_sent = True
                else:
                    messages = payload["messages"]
                    last = messages[-1] if messages else None
                    # messages 只含 ReAct 语料（critic 的校验在嵌套图里跑，不写进本图）：
                    # 末尾不带 tool_calls 的 assistant 消息 = React 轮的答案
                    if isinstance(last, AIMessage) and last.id not in recorded:
                        recorded.add(last.id)
                        if not last.tool_calls:
                            answer = last.content if isinstance(last.content, str) else ""
                    # 轮次由 middleware 写回 state：工作记忆淘汰会删 messages，反推会丢轮次
                    rounds = payload.get("rounds") or rounds
                    # critic 改写过的答案写在收口钩子返回的 state 更新里，覆盖 React 轮的答案
                    if payload.get("answer") is not None:
                        answer = payload["answer"]
        except GraphRecursionError:
            # 触顶：answer 已有值说明收口钩子跑完了，答案照常下发，只是末尾到 END 超了额度
            if answer is None:
                logger.warning("[Agent:%s] 达到 %d 步上限仍未收敛，返回兜底文案", self.name, RECURSION_LIMIT)
        # 判空用 None 而非空串：不调工具轮次即使 content 为空也算收敛（返回空串），
        # 只有「跑满额度仍每轮调工具」才落到下面的兜底文案
        if answer is not None:
            # 最终答案按小块上发（前端逐块渲染，保持流式感）
            for j in range(0, len(answer), 40):
                await emit({"type": "token", "content": answer[j:j + 40]})
            logger.info("[Agent:%s] 本轮完成，返回最终答案（%d 字）", self.name, len(answer))
            return answer, rounds
        # 未收敛时兜底返回：给一句非空提示，避免最终输出空串
        return "抱歉，本轮查询了较多信息但未能整合成完整回答，请换个方式或精简需求后再试。", rounds
