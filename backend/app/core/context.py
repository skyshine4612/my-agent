# app/core/context.py
# 请求级上下文：用 contextvars 把 user_id 透传到工具执行层。
# 工具经 registry.call_raw 执行时，入参只有 LLM 给的 args，拿不到用户身份；
# read_file/grep 这类需要按用户隔离读文件的工具，通过这里 get 当前 user_id 做归属校验。
from contextvars import ContextVar

# 当前请求的 user_id（UUID 字符串）：service 层在 chat_stream 入口 set，工具层 get。
# 真实链路必然注入——身份由 get_current_user 本地验签得出，未登录在路由层就被 401 拦下。
# 默认空串只是未注入时的确定性兜底，不再有 anonymous 这种「看起来像合法用户」的降级身份。
current_user_id: ContextVar[str] = ContextVar("current_user_id", default="")
