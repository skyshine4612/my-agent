# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

多业务对话式 Agent 平台，前后端分离：

- **backend/**：FastAPI 应用，实现「通用 ReAct agent → 工具调用 → critic 事实校验」链路。真实数据：高德（POI/推荐菜，Web 服务 API）、天气/节假日/翻译/热榜/菜谱/营养（UAPIS，天气最多 7 天预报）、搜索（Tavily）经 HTTP 直连；12306 火车票经 ModelScope 托管 MCP；机票直连 variflight 官方 MCP，LLM 默认接阿里云百炼 DashScope（`qwen3.7-plus`）。持久化全在自托管 Supabase 的 Postgres（`asyncpg` + RLS），身份用 Supabase Auth（GoTrue）。
- **frontend/**：Vue 3 + Vite + Element Plus + Pinia 的单页应用，登录页 + 通用对话界面（会话列表 + 流式聊天 + 工具调用气泡）。

架构已从「旅行专用」链路收敛为「通用 agent + 标准 skill」两段式（见下）。

## 常用命令

后端（在 `backend/` 目录下，依赖由 `uv` 管理，`uv.lock` 已提交）：

```sh
uv sync                                              # 安装依赖
uv run python run.py                                 # 启动开发服务（uvicorn reload，端口 8000）
uv run pytest                                        # 跑全部测试
uv run pytest tests/test_llm.py::test_llm_contract   # 跑单个测试
```

- 测试配置在 `backend/pyproject.toml`：`asyncio_mode = "auto"`（async 测试无需 `@pytest.mark.asyncio`，但现有测试仍显式加了），`testpaths = ["tests"]`。
- 测试模式：不依赖真实 LLM / 网络，用 `Fake*LLM` 类注入 `service.llm`；数据层连真实 Postgres 测试库 `agent_test`（`SUPABASE_TEST_DATABASE_URL` 由 `tests/conftest.py` 的 `db` fixture 建池并在用例间清表），未配该变量时相关用例整体 skip。
- 配置：复制 `backend/.env.example` → `backend/.env` 填真实 key；未配 `LLM_API_KEY` 时后端仍可启动（`FallbackLLM` 返回空内容），未配 `SUPABASE_DATABASE_URL` 时启动会因建池失败而报错。

前端（在 `frontend/` 目录下）：

```sh
npm install
npm run dev          # vite 开发服务器，/api 代理到 http://127.0.0.1:8000
npm run build        # vue-tsc 类型检查 + vite 构建
npm run type-check   # 仅类型检查
```

整体部署：

```sh
docker compose up --build   # 前端 nginx 对外 :80，反代 /api → backend:8000；后端读挂载的 backend/.env
```

## 架构

### 后端请求链路（一次对话的完整路径）

```
路由层                    service 编排层                    核心执行层
api/routes/chat.py  →  services/agent_service.py  →  core/agent.py (ReAct 循环)
  POST /api/agent/chat    AgentService.chat_stream         Agent.run_stream
  (SSE 流)                装配会话历史/短期·长期记忆/LLM/工具/技能  → core/registry.py (工具执行)
                          + critic 校验 + LTM 提炼            → core/llm.py (LLM 客户端)
```

- **`app/services/agent_service.py`**：核心编排器，进程级单例 `service`（路由层直接 `import` 复用）。`chat_stream` 依次做：会话归属校验/新建 → 落库用户消息 → LTM 召回偏好 → 组装历史 → 注入 system prompt（含可用业务清单 + 事实规范 + 今天日期）→ 驱动 `Agent.run_stream` → critic 校验 → 落库结构化 assistant 消息 → 流式输出答案 → fire-and-forget 提炼长期记忆。记忆的组装/压缩/提炼委托给 `core/memory/manager.py` 的 `MemoryManager`。
- **`app/core/agent.py`**：`Agent.run_stream` 实现 ReAct 循环（LLM 决策 → 工具调用 → 回填结果 → 再决策，最多 `max_iters=20` 轮）。同一轮多个 tool_calls 用 `asyncio.gather` 并行执行；工具结果用 `call_raw` 拿结构化原始结果后做结构化截断（list 前 20 / dict 前 20 键 / str `truncate_limit` 默认 4000），完整结果 spill 到文件供 `read_file`/`grep` 读回；工作记忆（本轮工具结果）的 token 预算淘汰委托给 `core/memory/working.py` 的 `WorkingMemory`。
- **`app/core/registry.py`**：`ToolRegistry` 统一管理工具元信息（name/description/parameters/fn/label），转成 OpenAI function-calling schema 供 LLM 决策，并执行同步/异步工具函数。
- **`app/core/llm.py`**：`LLMClient` 抽象（`chat`/`complete`/`stream_chat` 三方法）+ `OpenAICompatLLM`（DashScope）+ `FallbackLLM`（无 key 兜底）+ `get_llm()` 单例工厂。
- **`app/core/critic.py`**：`run_critic` 事实校验专家——多轮带工具（`read_file`/`grep` 定向读完整落库结果做 grounded 校验），比对「工具结果 vs 回答」，输出 `{ok, issues}`；注入当天日期避免年份误判，过滤「correction 为空」的误报 issue，限轮数 + fail-open（失败/超限按通过处理，不阻塞主流程）。

### 关键机制 / 不变量

1. **critic 事实校验回路**（`agent_service.py` + `critic.py`）：仅当本轮调用了「硬数据工具」（`HARD_DATA_TOOLS = {train_ticket_query, flight_query, weather_query, holiday_calendar}`）才触发。第一轮答案先在 `consume()` 里被缓存、不直接下发；critic 判不通过时，**复用已查到的工具结果**让 LLM 直接重写答案（不重新调工具、不走 ReAct）。最终答案先落库（完整答案在拆块流式下发**之前**入库）、再拆成 40 字符块流式下发。所以 `token` 事件不是真正的逐 token 流式，而是「校验通过后分块补发」。落库与生成都跑在 `chat_stream` 里的独立 `run_full_turn` worker task 中，主协程只从事件队列转发 SSE——客户端刷新断开只取消主协程（转发），worker 照常跑完并落库，故「后端生成完、前端再刷新」仍能拉到本轮答案。critic 本体是「多轮带工具」：可用 `read_file`/`grep` 定向读回完整落库结果做事实比对（对齐 CRITIC/CoV），限 `max_iter=3` + fail-open；校验语料与模型生成时所见一致（`result` 截断版）。

2. **ToolRegistry 只在启动时装配一次**：`AgentService.__init__` 里 `_build_registry()` 一次性 `new` 所有数据源并缓存到 `self.registry`。原因：`Train12306DataSource` / `FlightVariflightDataSource` 及各 HTTP 数据源（UAPIS/Tavily/高德 Web）的 `__init__` 会创建从不 `close()` 的 httpx 客户端，若每请求重建会导致连接泄漏。**新增工具时注意保持这个「启动时装配一次」的模式**。

3. **用户隔离 = 登录 + RLS**：前端用 `supabase-js` 直连 GoTrue 登录/注册，拿到 JWT 后以 `Authorization: Bearer` 调后端；后端 `core/auth.py` 的 `get_current_user` 用 `SUPABASE_JWT_SECRET` 做本地 HS256 验签，取 `sub` 作为 `user_id`（未带/失效一律 401，无 `anonymous` 兜底）。数据侧是**双保险**：业务方法显式带 `user_id` 条件，同时每条查询都跑在 `core/db.py` 的 `rls_conn(user_id)` 事务里——事务内 `set_config('role','authenticated')` + 注入 `request.jwt.claims`（复刻 PostgREST 行为），让 RLS 策略真正生效。**注意 `postgres`/`service_role` 是 `BYPASSRLS` 角色，直连裸池跑查询策略完全不执行**，所以业务查询必须走 `rls_conn`，裸池 `admin_conn` 只给测试基础设施用。

4. **SSE 事件协议**：后端 `chat.py` 用 `sse-starlette` 的 `EventSourceResponse(gen(), sep="\n")`，`sep="\n"` 是刻意的——默认 `\r\n` 会导致前端按 `\n\n` 分帧永远匹配不到。事件 `type` 有：`conversation_id` / `token` / `tool_call` / `tool_result` / `status` / `done`。`tool_call` 与 `tool_result` 都携带 `id`（tool_call_id）：同名工具并行且乱序完成时，前端/后端按 `id` 而非工具名或队列顺序配对 summary，避免串配。

5. **标准 skill 两段式**（`core/skills.py` + `tools/system.py`）：`app/skills/<name>/SKILL.md` 的 YAML frontmatter（name + description）常驻 system prompt 的 `{skill_directory}` 插槽；正文由 `get_skill` 工具按需加载（其 `name` 参数用 `enum` 约束只能取已注册业务）。新增业务 = 在 `app/skills/` 下加一个目录 + `SKILL.md`，无需改 service 装配代码。

6. **提示词集中在 `app/prompts/*.md`**：`system.md`（含 `{skill_directory}` / `{tool_grounding}` 两个插槽，由 `render_system_prompt` 用 `str.replace` 填充）、`grounding.md`（严禁编造事实数据）、`critic.md`（critic 输出 JSON 契约）。`today_hint()` 会注入当前日期，避免 LLM 编造过去日期。

7. **数据源分层**：两个基类——`datasource/mcp_base.py` 的 `McpDataSource` 封装 MCP 的「懒加载持久 session + 超时 + 失效重建」（12306 / 机票继承）；`datasource/http_base.py` 的 `HttpDataSource` 封装 httpx 异步客户端（高德 Web / UAPIS / Tavily 继承）。`amap_web.py` 把高德返回解析成约定结构（纯函数 `parse_poi_list` / `parse_weather` 便于单测）。

8. **配置**：`app/config.py` 用 pydantic-settings 从 `backend/.env` 读取（`LLM_*`、`MODELSCOPE_TOKEN`、各 `*_MCP_URL`、`UAPIS_API_KEY`、`TAVILY_API_KEY`、`AMAP_API_KEY`、`VARIFLIGHT_API_KEY`、`SUPABASE_DATABASE_URL`、`SUPABASE_TEST_DATABASE_URL`、`SUPABASE_JWT_SECRET`）。容器内通过 compose 挂载 `./backend/.env` 到 `/app/.env`，密钥类参数不写进 compose。前端另有 `frontend/.env`（`VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` / `VITE_AMAP_WEB_JS_KEY`），`VITE_` 前缀的变量在 `npm run build` 时被内联进产物，所以 Docker 构建前必须先备好该文件。

9. **子 Agent 委派（上下文隔离）**（`tools/sub_agent.py`）：顶层 agent 可用 `call_sub_agent` 工具把复杂子任务（如查交通、查景点）委派给独立上下文的子 Agent——子 Agent 历史为空、只吃 `task`，业务规则由它按需 `get_skill` 加载，跑完只回传最终答案摘要，主 agent 的上下文不被大量工具结果淹没。子 Agent 内部工具调用不上报前端（`on_event=None`），前端只显示「委派子任务」一个气泡。当前为单层委派（子 Agent 工具集不含 `call_sub_agent`，避免无限下钻）。子 Agent 分配独立 `sub_conversation_id`（uuid）隔离工具结果地址索引（spill 目录），任务结束（含异常）即清理，不与主会话/其他子 Agent 串扰。

10. **三层记忆 + token 预算 + 三层压缩**（`core/memory/` 包）：工作记忆（本轮工具结果，内存，每次问答即丢）+ 短期记忆（`short_term_memory` 表，喂 LLM 的跨轮上下文权威源：1 条 summary 摘要 + 结构化消息序列（user / assistant 含 tool_calls / tool），跨轮持久）+ 长期记忆（`long_term_memory` 表，按 `user_id` 跨会话）+ 会话历史（`conversation.py`，messages/conversations 仅前端展示）。记忆编排统一在 `memory/manager.py` 的 `MemoryManager`（组装/压缩/写回/提炼），四个 store 由 `AgentService` 创建后共享；四张表的连接统一走 `core/db.py`（asyncpg 连接池，lifespan 里建池/关池，业务查询走 `rls_conn` 切角色开事务）。四个 store 的 `__init__` 一律零 IO——进程级单例 `service` 在 import 时就构造 store，而连接池要到 lifespan 才建，任何构造期建表/连库都会直接炸。token 预算用 `tiktoken`（cl100k_base）真实计数，失败回退启发式（`token_utils.py`）。短期记忆超 token 预算时早期回合压成累积摘要（存表 + 落盘 `_history.txt`）；长期记忆 fire-and-forget 只提炼本轮 user 消息（per-user 锁串行，靠去重时 importance 提升体现「稳定」）；工作记忆单个结果超长时 spill（结构化截断 + 文件地址索引），多结果累积超预算时成对淘汰最老 + LLM 蒸馏兜底（蒸馏失败降级为拼接截断，不炸整轮）。命名约定：工作/短期/长期记忆用 `working`/`short_term`/`long_term`，会话历史用 `conversation`，禁止 `session` 等混用。

11. **工具结果地址索引（spill-to-file）**（`core/memory/tool_result.py` + `tools/file.py`）：工具结果超 `truncate_limit`（默认 4000 字符）时，完整结果写临时文件 `result_dir/<user_id>/<conversation_id>/<uuid>.txt`，窗口只放「预览 + 文件路径提示」，模型用 `read_file(path, offset, limit)`（顺序读 + 未读完进度提示）与 `grep(pattern, path)`（正则搜 + 位置/上下文片段）两个**写死的安全工具**按需读回被省略的部分。path 经路径白名单校验（拒绝 `..`/绝对路径穿越），按 `current_user_id`（`core/context.py` 的 contextvar）隔离目录。生命周期：spill 文件本轮结束即清（保留 `_history.txt`），`delete_conversation` 删该会话目录时统一清理；子 Agent 同样写文件、子任务结束即清。这是对「纯截断」的升级——被截掉的部分不再是永久丢失，而是可读回。

12. **短期记忆组装 + 压缩（running summary）**（`memory/manager.py` + `short_term.py`）：一个会话的短期记忆直接存 `short_term_memory` 表（多行：1 条 `summary` + 结构化消息序列 `user / assistant(含 tool_calls) / tool`），是喂 LLM 的跨轮上下文的权威源，**不依赖 messages 表**（messages/conversations 仅前端展示）。每轮生成后 `append_turn` 把本轮按 ReAct 轮次写进表（user + 每轮 assistant(tool_calls) + tool×N 占位 + assistant 答案，tool 结果不存、content 空串）；组装时预算驱动（`llm_context_budget * history_budget_ratio`，默认 50%）：早期回合压成累积摘要（序列化 + 蒸馏 + 追加 `_history.txt`），并**物理删除**早期消息（删除即增量，无 `compressed_through` marker），最近几轮保留。摘要注入 system prompt，最近几轮结构化消息作为短期记忆注入对话，拼上下文 = system「`[摘要] {summary}\n[完整历史存到 {path}，可用 read_file 读]`」+ 最近几轮结构化消息 + 本轮工作记忆（工具结果）。这样模型跨轮能看到「查过哪些工具、什么参数」，对没查过的对象重新调工具（grounding 强制每轮重新调）；被压缩的早期回合答案落盘 `_history.txt` 可 `read_file` 读回。

### 前端结构

- `views/Chat.vue`：主界面，负责 SSE 事件分发（`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`）与会话管理。
- 记忆以弹窗形式内置于 `views/Chat.vue`：输入框旁「记忆」入口，`el-tabs` 切换长期记忆（跨会话偏好）与短期记忆（摘要 + 最近几轮对话）。
- `components/ChatPanel.vue`：渲染 markdown（`marked` + `DOMPurify` 防 XSS）+ 工具气泡 + 输入框。
- `services/supabase.ts`：Supabase 客户端单例 + `getAccessToken()`（`getSession` 会自动刷新即将过期的 token），是前端取登录凭证的唯一出口。
- `services/sse.ts`：手写 `fetch` 流式解析 SSE（规范化换行后按 `\n\n` 分帧，处理多字节字符切断）；注入 `Authorization: Bearer`，401 抛 `AuthExpiredError` 由调用方引导重新登录。
- `services/api.ts`：axios 封装，请求拦截器异步取 token 注入 `Authorization: Bearer`。
- `stores/auth.ts`：Pinia store，GoTrue 会话 + 登录/注册/登出；`init()` 读初始会话并订阅 `onAuthStateChange`。**用户面对的是用户名登录**——GoTrue 只认 email/phone，`signIn`/`signUp` 收到的用户名在 store 内拼成 `<用户名>@my-agent.local` 再提交（`USERNAME_DOMAIN`），所以 `auth.users.email` 存的是这个假邮箱；`displayName` 反向剥掉后缀供界面展示。
- `stores/conversation.ts`：Pinia store，会话列表 + 当前会话 id。
- `router/index.ts`：`/`（需登录）+ `/login`，全局 `beforeEach` 守卫等 `auth.init()` 完成后按会话有无双向重定向。
- 历史 assistant 消息在库里存成 JSON `{"content", "tools"}`，前端 `historyToMessages` 还原为 markdown + 工具气泡。