# app/core/context.py
# 请求级上下文：用 contextvars 把 user_id 透传到工具执行层。
# 工具经 registry.call_raw 执行时，入参只有 LLM 给的 args，拿不到用户身份；
# read_file/grep 这类需要按用户隔离读文件的工具，通过这里 get 当前 user_id 做归属校验。
from contextvars import ContextVar

# 当前请求的 user_id（UUID 字符串）：service 层在 chat_stream 入口 set，工具层 get。
# 真实链路必然注入——身份由 get_current_user 本地验签得出，未登录在路由层就被 401 拦下。
# 默认空串只是未注入时的确定性兜底，不再有 anonymous 这种「看起来像合法用户」的降级身份。
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")

# 当前请求的 conversation_id：工具层用它定位会话目录（读上传件、写产物）。
# 同样由 service 层在 chat_stream 入口 set；worker task 经 create_task 复制上下文，工具执行时读得到。
current_conversation_id: ContextVar[str] = ContextVar("current_conversation_id", default="")

# 本轮的事件回调（run_stream 的 on_event）：由 Agent.run_stream 在开跑前 set。
# call_sub_agent 需要把子 Agent 的工具调用沿同一条 SSE 通道转发出去，但它由 service 在启动时
# 装配一次（见 CLAUDE.md 不变量 2），拿不到每轮才有的 on_event，故经 contextvar 透传。
current_emit: ContextVar[object] = ContextVar("current_emit", default=None)

# 正在执行的工具调用的 tool_call_id：由 _ToolExecMiddleware 在调工具前 set。
# call_sub_agent 用它当子任务的归属键——子 Agent 的事件带上它，前端才知道该挂到哪次委派下面。
# 同一个 task 的 context 副本保证并行工具互不串（框架用 asyncio.gather，每个 tool_call 各自一个 Task）。
current_tool_call_id: ContextVar[str] = ContextVar("current_tool_call_id", default="")
