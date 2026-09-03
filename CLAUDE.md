# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

多业务对话式 Agent 平台，前后端分离：

- **backend/**：FastAPI 应用，实现「通用 ReAct agent → 工具调用 → critic 事实校验」链路。真实数据：高德（POI/推荐菜，Web 服务 API）、天气/节假日/翻译/热榜/菜谱/营养（UAPIS，天气最多 7 天预报）、搜索（Tavily）经 HTTP 直连；12306 火车票经 ModelScope 托管 MCP；机票直连 variflight 官方 MCP，LLM 默认接阿里云百炼 DashScope（`qwen-plus`）。
- **frontend/**：Vue 3 + Vite + Element Plus + Pinia 的单页应用，通用对话界面（会话列表 + 流式聊天 + 工具调用气泡）。

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
- 测试模式：不依赖真实 LLM / 网络，用 `Fake*LLM` 类注入 `service.llm`，并用 `monkeypatch.setattr(settings, "db_path", str(tmp_path / "x.db"))` 隔离 SQLite。
- 配置：复制 `backend/.env.example` → `backend/.env` 填真实 key；未配 `LLM_API_KEY` 时后端仍可启动（`FallbackLLM` 返回空内容）。

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

- **`app/services/agent_service.py`**：核心编排器，进程级单例 `service`（路由层直接 `import` 复用）。`chat_stream` 依次做：会话归属校验/新建 → 落库用户消息 → LTM 召回偏好 → 组装历史 → 注入 system prompt（含可用业务清单 + 事实规范 + 今天日期）→ 驱动 `Agent.run_stream` → critic 校验 → 落库结构化 assistant 消息 → 流式输出答案 → fire-and-forget 提炼长期记忆。
- **`app/core/agent.py`**：`Agent.run_stream` 实现 ReAct 循环（LLM 决策 → 工具调用 → 回填结果 → 再决策，最多 `max_iters=20` 轮）。同一轮多个 tool_calls 用 `asyncio.gather` 并行执行；工具结果用 `call_raw` 拿结构化原始结果后做结构化截断（list 前 3 / dict 前 10 键 / str 2000，`full` 字段保留完整结果供 critic 用），工作记忆（本轮工具结果）的 token 预算淘汰委托给 `core/memory/working.py` 的 `WorkingMemory`。
- **`app/core/registry.py`**：`ToolRegistry` 统一管理工具元信息（name/description/parameters/fn/label），转成 OpenAI function-calling schema 供 LLM 决策，并执行同步/异步工具函数。
- **`app/core/llm.py`**：`LLMClient` 抽象（`chat`/`complete`/`stream_chat` 三方法）+ `OpenAICompatLLM`（DashScope）+ `FallbackLLM`（无 key 兜底）+ `get_llm()` 单例工厂。
- **`app/core/critic.py`**：`run_critic` 事实校验专家——多轮带工具（`read_result` 定向读完整落库结果做 grounded 校验），比对「工具结果 vs 回答」，输出 `{ok, issues}`；注入当天日期避免年份误判，过滤「correction 为空」的误报 issue，限轮数 + fail-open（失败/超限按通过处理，不阻塞主流程）。

### 关键机制 / 不变量

1. **critic 事实校验回路**（`agent_service.py` + `critic.py`）：仅当本轮调用了「硬数据工具」（`HARD_DATA_TOOLS = {train_ticket_query, flight_query, weather_query, holiday_calendar}`）才触发。第一轮答案先在 `consume()` 里被缓存、不直接下发；critic 判不通过时，**复用已查到的工具结果**让 LLM 直接重写答案（不重新调工具、不走 ReAct）。最终答案才拆成 40 字符块流式下发。所以 `token` 事件不是真正的逐 token 流式，而是「校验通过后分块补发」。critic 本体是「多轮带工具」：可用 `read_result` 定向读回完整落库结果做事实比对（对齐 CRITIC/CoV），限 `max_iter=3` + fail-open；校验语料与模型生成时所见一致（`visible` 截断版）。

2. **ToolRegistry 只在启动时装配一次**：`AgentService.__init__` 里 `_build_registry()` 一次性 `new` 所有数据源并缓存到 `self.registry`。原因：`Train12306DataSource` / `FlightVariflightDataSource` 及各 HTTP 数据源（UAPIS/Tavily/高德 Web）的 `__init__` 会创建从不 `close()` 的 httpx 客户端，若每请求重建会导致连接泄漏。**新增工具时注意保持这个「启动时装配一次」的模式**。

3. **用户隔离**：所有路由从请求头 `X-User-Id`（缺省 `anonymous`）解析用户身份，透传给 `ConversationStore` / `LongTermMemory`，所有读写/删除/淘汰都按 `user_id` 过滤，防止跨用户读写注入。前端在 localStorage 生成 `client_id` 并通过 header 注入（`services/api.ts` 的 axios 拦截器 + `sse.ts` 手动加 header）。

4. **SSE 事件协议**：后端 `chat.py` 用 `sse-starlette` 的 `EventSourceResponse(gen(), sep="\n")`，`sep="\n"` 是刻意的——默认 `\r\n` 会导致前端按 `\n\n` 分帧永远匹配不到。事件 `type` 有：`conversation_id` / `token` / `tool_call` / `tool_result` / `status` / `done`。`tool_call` 与 `tool_result` 都携带 `id`（tool_call_id）：同名工具并行且乱序完成时，前端/后端按 `id` 而非工具名或队列顺序配对 summary，避免串配。

5. **标准 skill 两段式**（`core/skills.py` + `tools/system.py`）：`app/skills/<name>/SKILL.md` 的 YAML frontmatter（name + description）常驻 system prompt 的 `{skill_directory}` 插槽；正文由 `get_skill` 工具按需加载（其 `name` 参数用 `enum` 约束只能取已注册业务）。新增业务 = 在 `app/skills/` 下加一个目录 + `SKILL.md`，无需改 service 装配代码。

6. **提示词集中在 `app/prompts/*.md`**：`system.md`（含 `{skill_directory}` / `{tool_grounding}` 两个插槽，由 `render_system_prompt` 用 `str.replace` 填充）、`grounding.md`（严禁编造事实数据）、`critic.md`（critic 输出 JSON 契约）。`today_hint()` 会注入当前日期，避免 LLM 编造过去日期。

7. **数据源分层**：两个基类——`datasource/mcp_base.py` 的 `McpDataSource` 封装 MCP 的「懒加载持久 session + 超时 + 失效重建」（12306 / 机票继承）；`datasource/http_base.py` 的 `HttpDataSource` 封装 httpx 异步客户端（高德 Web / UAPIS / Tavily 继承）。`amap_web.py` 把高德返回解析成约定结构（纯函数 `parse_poi_list` / `parse_weather` 便于单测）。

8. **配置**：`app/config.py` 用 pydantic-settings 从 `backend/.env` 读取（`LLM_*`、`MODELSCOPE_TOKEN`、各 `*_MCP_URL`、`UAPIS_API_KEY`、`TAVILY_API_KEY`、`AMAP_API_KEY`、`VARIFLIGHT_API_KEY`、`DB_PATH`）。容器内通过 compose 挂载 `./backend/.env` 到 `/app/.env`，密钥类参数不写进 compose。

9. **子 Agent 委派（上下文隔离）**（`tools/sub_agent.py`）：顶层 agent 可用 `call_sub_agent` 工具把复杂子任务（如查交通、查景点）委派给独立上下文的子 Agent——子 Agent 历史为空、只吃 `task`，业务规则由它按需 `get_skill` 加载，跑完只回传最终答案摘要，主 agent 的上下文不被大量工具结果淹没。子 Agent 内部工具调用不上报前端（`on_event=None`），前端只显示「委派子任务」一个气泡。当前为单层委派（子 Agent 工具集不含 `call_sub_agent`，避免无限下钻）。子 Agent 分配独立 `sub_conversation_id`（uuid）隔离工具结果地址索引（spill 目录），任务结束（含异常）即清理，不与主会话/其他子 Agent 串扰。

10. **三层记忆 + token 预算 + 三层压缩**（`core/memory/` 包）：工作记忆（本轮工具结果，内存，每次问答即丢）+ 短期记忆（`short_term_memory` 表，多行：1 条 summary 摘要 + 最近几轮对话原文，跨轮持久）+ 长期记忆（`long_term_memory` 表，按 `user_id` 跨会话）+ 会话历史（`conversation.py`）。短期记忆超 token 预算时早期回合压成累积摘要（存表 + 落盘 `_history.txt`）；工作记忆单个结果超长时 spill（结构化截断 + 文件地址索引），多结果累积超预算时成对淘汰最老 + LLM 蒸馏兜底。命名约定：工作/短期/长期记忆用 `working`/`short_term`/`long_term`，会话历史用 `conversation`，禁止 `session` 等混用。

11. **工具结果地址索引（spill-to-file）**（`core/memory/tool_result.py` + `tools/file.py`）：工具结果超 `truncate_limit`（默认 4000 字符）时，完整结果写临时文件 `result_dir/<user_id>/<conversation_id>/<uuid>.txt`，窗口只放「预览 + 文件路径提示」，模型用 `read_file(path, offset, limit)`（顺序读 + 未读完进度提示）与 `grep(pattern, path)`（正则搜 + 位置/上下文片段）两个**写死的安全工具**按需读回被省略的部分。path 经路径白名单校验（拒绝 `..`/绝对路径穿越），按 `current_user_id`（`core/context.py` 的 contextvar）隔离目录。生命周期：本轮结束（`done` 前）与 `delete_conversation` 删该会话目录；子 Agent 同样写文件、子任务结束即清。这是对「纯截断」的升级——被截掉的部分不再是永久丢失，而是可读回。

12. **历史会话压缩（running summary，短期记忆 = 摘要 + 最近几轮原文）**（`agent_service.py` + `short_term.py`）：一个会话的短期记忆存 `short_term_memory` 表（多行：1 条 `summary` 摘要 + 最近几轮对话原文），完整历史文件落盘 `result_dir/<user_id>/<conv_id>/_history.txt`。组装时预算驱动（`llm_context_budget * history_budget_ratio`，默认 50%）：早期回合压成累积摘要（存表 + 追加历史文件），最近几轮保留原文（存表）。摘要固定注入 system prompt，最近几轮原文作为短期记忆注入对话，拼上下文 = system「`[摘要] {summary}\n[完整历史存到 {path}，可用 read_file 读]`」+ 最近几轮原文 + 本轮工作记忆（工具结果）。这样「压缩把跨轮编排结果（如『第8天=什锦烫饭』）蒸馏丢」的幻觉，被「完整历史落盘 + read_file 读回」兜住。

### 前端结构

- `views/Chat.vue`：主界面，负责 SSE 事件分发（`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`）与会话管理。
- 记忆以弹窗形式内置于 `views/Chat.vue`：输入框旁「记忆」入口，`el-tabs` 切换长期记忆（跨会话偏好）与短期记忆（摘要 + 最近几轮对话）。
- `components/ChatPanel.vue`：渲染 markdown（`marked` + `DOMPurify` 防 XSS）+ 工具气泡 + 输入框。
- `services/sse.ts`：手写 `fetch` 流式解析 SSE（规范化换行后按 `\n\n` 分帧，处理多字节字符切断）。
- `services/api.ts`：axios 封装，统一注入 `X-User-Id`。
- `stores/conversation.ts`：Pinia store，会话列表 + 当前会话 id。
- 历史 assistant 消息在库里存成 JSON `{"content", "tools"}`，前端 `historyToMessages` 还原为 markdown + 工具气泡。