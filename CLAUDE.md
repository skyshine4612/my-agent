# CLAUDE.md

写给**在本仓库改代码的 agent**：能跑的命令、架构，以及**改了会静默出错的不变量**。

收录判据：换台机器、换个环境依然成立，且光读代码看不出来。部署动作、主机地址、环境变量取值不属于这里（分别归 `docker/` 部署包与 gitignore 的 `.env`）。

## 项目概览

多业务对话式 Agent 平台，前后端分离：

- **backend/**：FastAPI 应用，实现「通用 ReAct agent → 工具调用 → critic 事实校验」链路。真实数据：高德（POI/推荐菜，Web 服务 API）、天气/节假日/翻译/热榜/菜谱/营养（UAPIS，天气最多 7 天预报）、搜索（Tavily）经 HTTP 直连；12306 火车票经 ModelScope 托管 MCP；机票直连 variflight 官方 MCP，LLM 默认接阿里云百炼 DashScope（`qwen3.7-plus`）。持久化全在自托管 Supabase 的 Postgres（`asyncpg` + RLS），身份用 Supabase Auth（GoTrue）。另有本地知识库（RAG）：攻略语料切块后用 `text-embedding-v4` 向量化入库（pgvector），`search_knowledge` 工具按余弦相似度检索。
- **frontend/**：Vue 3 + Vite + Element Plus + Pinia 的单页应用，登录页 + 通用对话界面（会话列表 + 流式聊天 + 工具调用气泡）。

## 常用命令

后端（在 `backend/` 目录下，依赖由 `uv` 管理，`uv.lock` 已提交）：

```sh
uv sync                                              # 安装依赖
uv run python run.py                                 # 启动开发服务（uvicorn reload，端口 8000）
uv run pytest                                        # 跑全部测试
uv run pytest tests/test_llm_model.py::test_get_chat_model_is_singleton   # 跑单个测试
```

- 异步测试不必写 `@pytest.mark.asyncio`（`asyncio_mode = "auto"`），但现有用例仍显式加了。
- 测试模式：不依赖真实 LLM / 网络，用假 langchain 模型注入 `service.chat_model`；数据层连真实 Postgres 测试库 `agent_test`（`SUPABASE_TEST_DATABASE_URL` 由 `tests/conftest.py` 的 `db` fixture 建池并在用例间清表），未配该变量时相关用例整体 skip。
- 配置：复制 `backend/.env.example` → `backend/.env` 填真实 key；未配 `LLM_API_KEY` 时后端仍可启动（`get_chat_model()` 返回产出空内容的假模型），未配 `SUPABASE_DATABASE_URL` 时启动会因建池失败而报错。
- 知识库的「抓取网址」入口依赖 Playwright 浏览器，首次需 `uv run playwright install chromium`（约 700MB）；Linux 缺系统库时再执行 `uv run playwright install-deps chromium`。未装时粘贴文本 / 上传文件入口照常可用，只有抓网址会失败（接口会返回安装指引）。

前端（在 `frontend/` 目录下）：

```sh
npm install
npm run dev          # vite 开发服务器，/api 代理到 http://127.0.0.1:8000
npm run build        # vue-tsc 类型检查 + vite 构建
npm run type-check   # 仅类型检查
```

## 架构

### 后端请求链路（一次对话的完整路径）

```
路由层                    service 编排层                    核心执行层
api/routes/chat.py  →  services/agent_service.py  →  core/agent.py (ReAct 循环)
  POST /api/agent/chat    AgentService.chat_stream         Agent.run_stream
  (SSE 流)                装配会话历史/短期·长期记忆/LLM/工具/技能  → core/registry.py (工具执行)
                          + 知识库检索 + critic 校验 + LTM 提炼  → core/llm.py (模型工厂)
```

- **`app/core/agent.py`**：`Agent.run_stream` 实现 ReAct 循环，图侧额度 `RECURSION_LIMIT` 固定 60 步（一轮 ReAct 占 3 步，即最多 20 轮）；同一轮多个 tool_calls 由框架的 tools 节点并行执行。
- **`app/core/registry.py`**：`ToolRegistry` 统一管理工具元信息（name/description/fn/label）并执行工具函数。模型侧的 schema 不在这里生成——langchain 装配时从各 tool 对象的 pydantic `args_schema` 直接转，中间无后处理。
- **`app/core/llm.py`**：全项目唯一的 LLM 通路。`get_chat_model()` 单例工厂返回 langchain 模型（DashScope 走 OpenAI 兼容协议，故用 `ChatOpenAI`）；`close_chat_model()` 由 lifespan 调用，关闭模型持有的 httpx 连接池。

### 关键机制 / 不变量

1. **critic 事实校验回路**（`agent.py` + `agent_service.py` + `critic.py`）：仅当本轮调用了「硬数据工具」（`HARD_DATA_TOOLS = {train_ticket_query, flight_query, weather_query, holiday_calendar}`）才触发。回路挂在 `create_agent` 的收口钩子 `aafter_agent` 上（收口钩子是图里的出口节点、占 1 步额度；钩子内的校验与改写是普通 async 调用，不新增图节点）；收口链按「middleware 列表末尾 → 列表开头」依次执行，critic 挂在末尾才最先跑，测试里有护栏钉住这个位置。ReAct 轮答案先在 `consume()` 里被缓存、不直接下发；critic 判不通过时，**复用已查到的工具结果**让 LLM 直接重写答案（不重新调工具、不走 ReAct），改完回头再校验，**是环不是一次性**——最多改写 `CriticConfig.max_rewrite`（默认 2）次，故 critic 最多跑 3 轮。最终答案先落库（完整答案在拆块流式下发**之前**入库）、再拆成 40 字符块流式下发。所以 `token` 事件不是真正的逐 token 流式，而是「校验通过后分块补发」。落库与生成都跑在 `chat_stream` 里的独立 `run_full_turn` worker task 中，主协程只从事件队列转发 SSE——客户端刷新断开只取消主协程（转发），worker 照常跑完并落库，故「后端生成完、前端再刷新」仍能拉到本轮答案。critic 本体是「多轮带工具」：可用 `read_file`/`grep` 定向读回完整落库结果做事实比对（对齐 CRITIC/CoV），限 `max_iter=3` + fail-open；校验语料与模型生成时所见一致（`result` 截断版）。

2. **ToolRegistry 只在启动时装配一次**：`AgentService.__init__` 里 `_build_registry()` 一次性 `new` 所有数据源并缓存到 `self.registry`。原因：`Train12306DataSource` / `FlightVariflightDataSource` 及各 HTTP 数据源（UAPIS/Tavily/高德 Web）的 `__init__` 会创建从不 `close()` 的 httpx 客户端，若每请求重建会导致连接泄漏。**新增工具时注意保持这个「启动时装配一次」的模式**。

3. **用户隔离 = 登录 + RLS**：前端用 `supabase-js` 直连 GoTrue 登录/注册，拿到 JWT 后以 `Authorization: Bearer` 调后端；后端 `core/auth.py` 的 `get_current_user` 用 `SUPABASE_JWT_SECRET` 做本地 HS256 验签，取 `sub` 作为 `user_id`（未带/失效一律 401，无 `anonymous` 兜底）。数据侧是**双保险**：业务方法显式带 `user_id` 条件，同时每条查询都跑在 `core/db.py` 的 `rls_conn(user_id)` 事务里——事务内 `set_config('role','authenticated')` + 注入 `request.jwt.claims`（复刻 PostgREST 行为），让 RLS 策略真正生效。**注意 `postgres`/`service_role` 是 `BYPASSRLS` 角色，直连裸池跑查询策略完全不执行**，所以业务查询必须走 `rls_conn`，裸池 `admin_conn` 只给测试基础设施用。**storage 链路被拒（后端每次调用现签的 HS256 JWT 被 storage-api 拒）是「有人动了 auth 密钥」的第一信号**——见 `core/storage.py` 的告警日志，同时确认没有启用 Supabase 非对称密钥。

4. **SSE 事件协议**：后端 `chat.py` 用 `sse-starlette` 的 `EventSourceResponse(gen(), sep="\n")`，`sep="\n"` 是刻意的——默认 `\r\n` 会导致前端按 `\n\n` 分帧永远匹配不到。事件 `type` 有：`conversation_id` / `token` / `tool_call` / `tool_result` / `status` / `done`。`tool_call` 与 `tool_result` 都携带 `id`（tool_call_id）：同名工具并行且乱序完成时，前端/后端按 `id` 而非工具名或队列顺序配对 summary，避免串配。

5. **标准 skill 两段式**（`core/skills.py` + `tools/system.py`）：`app/skills/<name>/SKILL.md` 的 YAML frontmatter（name + description）常驻 system prompt 的 `{skill_directory}` 插槽；正文由 `get_skill` 工具按需加载（其 `name` 参数用 `enum` 约束只能取已注册业务）。新增业务 = 在 `app/skills/` 下加一个目录 + `SKILL.md`，无需改 service 装配代码。**`## 输出格式` 章节照 Anthropic 的 Template pattern 写**：把结构包进 markdown 代码块（比散文描述可靠，缺了它会每次换排版），块内**只放字面输出与 `{占位符}`，一句说明都不要放**——实测把「逐日一行」「一行一家，招牌菜只能来自 poi_detail」这类说明写进块里，模型会当内容照抄进答案（块外用散文写同样的说明则不会）。块的严格度用「严格按下面这个模板输出，标题顺序层级一字不改」；多套方案的写法（哪几节每套各写一遍、哪几节全篇一份）写在块外。**规则正文里让模型「列出」的东西，模板里必须有对应小节**——`travel` 的规则要求景点用 `poi_search` 查、住宿「列出名称与位置」，模板就要有 `**景点**` / `**住宿**` 两节；漏了模型只能自己发挥，正是模板要消灭的「每次换排版」。

6. **提示词集中在 `app/prompts/*.md`**：`system.md`（含 `{skill_directory}` / `{tool_grounding}` 两个插槽，由 `render_system_prompt` 用 `str.replace` 填充）、`grounding.md`（严禁编造事实数据）、`critic.md`（critic 输出 JSON 契约）、`vision.md`（图片描述与「有无有效信息」判定，含 `{title}` 插槽）。`today_hint()` 会注入当前日期，避免 LLM 编造过去日期。

7. **数据源分层**：两个基类——`datasource/mcp_base.py` 的 `McpDataSource` 封装 MCP 的「懒加载持久 session + 超时 + 失效重建」（12306 / 机票继承）；`datasource/http_base.py` 的 `HttpDataSource` 封装 httpx 异步客户端（高德 Web / UAPIS / Tavily 继承）。`amap_web.py` 把高德返回解析成约定结构（纯函数 `parse_poi_list` / `parse_weather` 便于单测）。

8. **配置**：`app/config.py` 用 pydantic-settings 从 `backend/.env` 读取（`LLM_*`、`EMBEDDING_*`、`VISION_*`、`MODELSCOPE_TOKEN`、`TRAIN_12306_URL`、`FLIGHT_VARIFLIGHT_URL`、`UAPIS_API_KEY`、`TAVILY_API_KEY`、`AMAP_API_KEY`、`VARIFLIGHT_API_KEY`、`SUPABASE_DATABASE_URL`、`SUPABASE_TEST_DATABASE_URL`、`SUPABASE_JWT_SECRET`、`SUPABASE_URL`、`SANDBOX_URL`、`SANDBOX_TIMEOUT`）。`EMBEDDING_API_KEY`/`VISION_API_KEY`/`VISION_MODEL` 必须显式配（值通常与 `llm_*` 相同，但代码不做复用兜底——兜底会让「没配」和「配错了」长得一样），`EMBEDDING_DIMENSIONS` 必须与建表的 `vector(N)` 一致，换 embedding 模型时两处要一起改。后端进程直接读 `backend/.env`；密钥类参数不写进 compose。前端另有 `frontend/.env`（`VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` / `VITE_AMAP_WEB_JS_KEY`），`VITE_` 前缀的变量在 `npm run build` 时被内联进产物，故改了它们必须重新构建才生效。

9. **子 Agent 委派（上下文隔离）**（`tools/sub_agent.py` + 各业务的 `skills/<业务>/SKILL.md`）：顶层 agent 可用 `call_sub_agent` 工具把整块独立工作委派给独立上下文的子 Agent——子 Agent 历史为空、只吃 `task`，业务规则由它按需 `get_skill` 加载，跑完只回传最终答案，主 agent 的上下文不被大量工具结果淹没。**它必须自己带 critic**：主 agent 这轮调的是 `call_sub_agent`、不在 `HARD_DATA_TOOLS` 里，主 agent 的 `hard_results` 恒空、critic 不会触发，所以子 Agent 查到的硬数据只能靠它自己那一轮校验（`service` 装配时把同一个 `CriticConfig` 一并传进去）。**触发靠业务规则、不靠系统提示词**：`system.md` / `grounding.md` 里没有一句讲委派（这两处才是每轮恒可见的位置），触发判据写在**各业务** `SKILL.md` 的步骤里，认的场景只有两类——用户要 2-3 套方案、或一次规划多个目的地；判据是「子工作 ≥3-4 轮工具调用 / 中间结果父 agent 不要原始形态 / 子工作之间不共享约束」三条同时满足。**扇出粒度由业务自己的全局约束决定**：travel 按目的地或按套，`meal_planning` 只按套——**`meal_planning` 绝不能按天扇出**，「荤菜素菜全程不重样」是跨天全局约束，按天拆开各子任务互相看不见对方选了什么，必然重样。**「委派前先自己查共享数据」这一步只在输出模板真的需要那些字段时才加**：travel 有（模板强制 `**天气**` / `**交通**` 两节，必须先取回），`meal_planning` 没有（模板只有食谱表 / 采购清单 / 营养标注，不含节气）。照搬 travel 的写法给菜谱也加一句「先查一次节气和时令食材」会带偏模型：实测连续多轮反复调 `holiday_calendar`、烧掉一半步数额度也没走到委派。判据放 skill 而非 system.md 的理由：它是业务流程的一步、不是跨业务规范（两个业务的扇出粒度不同甚至相反），且这类请求必然先 `get_skill` 取输出模板才有法组装（实测短句「给我 2 套减脂食谱」首步也是 `get_skill`；第 13 条说的「短问题不触发 get_skill」指不需要业务流程的短问答，不适用）。真正保证「工具何时适用」的是 `call_sub_agent` 自己的工具描述，每轮随 schema 下发、与业务无关。**子 Agent 的进度按层级上报前端**：它由 service 启动时装配一次、拿不到每轮的 `on_event`，故 `core/context.py` 的 `current_emit` contextvar 在 `run_stream` 开跑前注入，子 Agent 取它当自己的 `on_event`，并在转发前给每条事件盖上两个归属章——`agent`（主 agent 为 `assistant`、子 Agent 为 `sub`）与 `parent`（发起它的那次 `call_sub_agent` 的 tool_call_id，来自 `current_tool_call_id` contextvar：`_ToolExecMiddleware` 在调工具前 set、`finally` 复位，并行工具各在自己的 Task 里有独立 context 副本，所以并行委派不会互相认领）。service 层原样透传并随 `tools` 落库，前端按 `parent` 把子任务的工具气泡与阶段状态挂到对应委派节点下。**委派节点靠 `title` 区分**：`call_sub_agent` 的 `title` 是**必填参数**（不填工具调用直接校验失败），不能拿 `task` 截断当名字——`task` 是给子 Agent 读的描述、天然越写越长，并行几个子任务很容易同名。**摘要要截断**：`call_sub_agent` 不在 `NON_QUERY_TOOLS` 里（那份白名单的前提是「有界且需要完整」，而摘要是 LLM 生成的无界散文），超 `truncate_limit` 照常走 spill。当前为单层委派（子 Agent 工具集不含 `call_sub_agent`，避免无限下钻）。子 Agent 分配独立 `sub_conversation_id`（uuid）隔离工具结果地址索引（spill 目录），任务结束（含异常）即清理，不与主会话/其他子 Agent 串扰。

10. **三层记忆 + token 预算 + 三层压缩**（`core/memory/` 包）：工作记忆（本轮工具结果，内存，每次问答即丢）+ 短期记忆（`short_term_memory` 表，喂 LLM 的跨轮上下文权威源：1 条 summary 摘要 + 结构化消息序列（user / assistant 含 tool_calls / tool），跨轮持久）+ 长期记忆（`long_term_memory` 表，按 `user_id` 跨会话，每条 fact 入库时向量化）+ 会话历史（`conversation.py`，messages/conversations 仅前端展示）。记忆编排统一在 `memory/manager.py` 的 `MemoryManager`（组装/压缩/写回/提炼），四个 store 由 `AgentService` 创建后共享；四张表的连接统一走 `core/db.py`（asyncpg 连接池，lifespan 里建池/关池，业务查询走 `rls_conn` 切角色开事务）。四个 store 的 `__init__` 一律零 IO——进程级单例 `service` 在 import 时就构造 store，而连接池要到 lifespan 才建，任何构造期建表/连库都会直接炸。token 预算用 `tiktoken`（cl100k_base）真实计数，失败回退启发式（`token_utils.py`）。短期记忆超 token 预算时早期回合压成累积摘要（存表 + 落盘 `_history.txt`）；长期记忆 fire-and-forget 只提炼本轮 user 消息（per-user 锁串行），召回按本轮问题语义检索（`recall(user_id, query=...)`，向量链路失败回退 importance 排序），写入前与该用户已有 fact 做语义去重（余弦 ≥ 0.85 视为同一条，取较大 importance 提升而非新增）；工作记忆单个结果超长时 spill（结构化截断 + 文件地址索引），多结果累积超预算时成对淘汰最老 + LLM 蒸馏兜底（蒸馏失败降级为拼接截断，不炸整轮）。命名约定：工作/短期/长期记忆用 `working`/`short_term`/`long_term`，会话历史用 `conversation`，禁止 `session` 等混用。**两个 LLM 蒸馏点（`manager.py` 的 `_update_summary`、`working.py` 的 `_distill`）的提示词都显式要求文件名原样保留**——文件名是重跑脚本恢复产物的唯一指针，被概括成「某个 Excel 文件」就断了，两句都有护栏测试钉住。

11. **工具结果地址索引（spill-to-file）**（`core/memory/tool_result.py` + `tools/file.py`）：工具结果超 `truncate_limit`（默认 4000 字符）时，完整结果写临时文件 `result_dir/<user_id>/<conversation_id>/<uuid>.txt`，窗口只放「预览 + 文件路径提示」，模型用 `read_file(path, offset, limit)`（顺序读 + 未读完进度提示）与 `grep(pattern, path)`（正则搜 + 位置/上下文片段）两个**写死的安全工具**按需读回被省略的部分。path 经路径白名单校验（拒绝 `..`/绝对路径穿越），按 `current_user_id`（`core/context.py` 的 contextvar）隔离目录。生命周期：spill 文件本轮结束即清（保留 `_history.txt`），`delete_conversation` 删该会话目录时统一清理；子 Agent 同样写文件、子任务结束即清。

12. **短期记忆组装 + 压缩（running summary）**（`memory/manager.py` + `short_term.py`）：一个会话的短期记忆直接存 `short_term_memory` 表（多行：1 条 `summary` + 结构化消息序列 `user / assistant(含 tool_calls) / tool`），是喂 LLM 的跨轮上下文的权威源，**不依赖 messages 表**（messages/conversations 仅前端展示）。每轮生成后 `append_turn` 把本轮按 ReAct 轮次写进表（user + 每轮 assistant(tool_calls) + tool×N 占位 + assistant 答案，tool 结果不存、content 空串）；组装时预算驱动（`llm_context_budget * history_budget_ratio`，默认 50%）：早期回合压成累积摘要（序列化 + 蒸馏 + 追加 `_history.txt`），并**物理删除**早期消息（删除即增量，无 `compressed_through` marker），最近几轮保留。摘要注入 system prompt，最近几轮结构化消息作为短期记忆注入对话，拼上下文 = system「`[摘要] {summary}\n[完整历史存到 {path}，可用 read_file 读]`」+ 最近几轮结构化消息 + 本轮工作记忆（工具结果）。这样模型跨轮能看到「查过哪些工具、什么参数」，对没查过的对象重新调工具（grounding 强制每轮重新调）；被压缩的早期回合答案落盘 `_history.txt` 可 `read_file` 读回。

13. **本地知识库（RAG）**（`core/knowledge.py` + `core/ingest.py` + `core/vision.py` + `tools/knowledge.py`）：`documents` 表存切块后的攻略语料（`chunk_index` 按来源聚合、`source_url` 是文档唯一标识），写入时用 `text-embedding-v4` 向量化，检索走 `match_documents(query_embedding, match_count)` 按余弦相似度取 top-k（`1 - (embedding <=> query)`），**再过一道相关性下限 `SIMILARITY_FLOOR`**（`knowledge.py` 的 `filter_relevant`）：那条 SQL 是纯 top-k、没有任何门槛，**库里只要有一行就必然返回 match_count 条**——问苏州、库里全是成都，也照样把成都的片段当「最相关」交给模型，再叠加 grounding 里「知识库优先级高于其他查询工具」，无关内容就被当权威资料写进答案，模型还会反复重搜找不存在的目标（实测 9 次）。门槛必须有，否则 `search_knowledge` 那句「知识库暂无相关内容」永远不可达、grounding 里「没有就改用专用工具」那条规则等于死指令。实测相关问句 ≥0.66、无关问句 ≤0.40，取 0.5；语料只有一篇文档时测的，语料变大后要重看。知识库记的是**用户自己收录的资料**，含 POI 接口给不出的细节（具体店名、营业时间、避坑经验），所以优先级高于通用查询工具——这条写进了 `prompts/grounding.md`（硬性规则，永远可见），`skills/travel/SKILL.md` 里另有一条讲与专用工具的分工。**注意规则不能只写在 SKILL.md 里**：短问题（如「河坊街有什么吃的」）不会触发 `get_skill`，业务规则正文压根不加载，只有 grounding 里的才总能生效。建库管道只在「知识库弹窗」触发、对话时一律不爬：粘贴文本/上传文件直接入库，URL 走 `ingest.py` 的 Playwright（跑完 JS 再取 DOM，攻略站多是前端渲染）+ trafilatura 提取正文（**除 `include_comments=False` 外都用 trafilatura 默认值**——攻略站评论区主要是「5分 超棒 举报点赞」这类点评，没有检索价值还会稀释正文块；**不覆盖 `MIN_EXTRACTED_SIZE`，用 trafilatura 默认的 250**——该阈值触发的是 `baseline()` 的扁平 `<p>` 纯文本替换：正文捞得更多，标题与图片一起没了。调到 800 只会让导航页/图集页丢掉标题与图片格式、不多捞正文；「抓得太少」trafilatura 2.2.0 自己会用 escalation 判断），入库前还有一道 `app/api/routes/knowledge.py` 的 `_MIN_CONTENT_LEN = 200` 拦截：真实短页面都在 270 字以上（澎湃详情 278、政府网政策 323），而抓不到时同样会产出「内容」——知乎限流页返回的是一段 117 字的 JSON 错误体，越过阈值就成了知识库里的噪声。图片位置留占位符——**单个私用区字符**（`ingest.py` 的 `image_mark(n)`，U+E000 起），正文提取后用 `vision.py` 拿多模态描述（模型由 `VISION_MODEL` 指定，`qwen3.7-plus` 本身支持图片输入）——知识库是文本语义检索，图片不转成文字就搜不到。**「这张图有没有用」的判定必须带页面标题做上下文**（`describe(context=…)`，由 `fetch_url_text` 把 title 传下去）：实测同一份图片字节不带标题时判定会摇摆（5 次里 4 次丢弃），带标题后 5 次全稳定；`temperature=0` 压不住服务端的 batch / 浮点非确定性，只作配合。判据也要客观——写「对行程规划有用」会让菜谱页/教程页的图因为「对行程没用」被判掉，得写「画面里有可辨认内容就描述，只有图标/广告位/头像/小程序码/纯色装饰条才回无有效信息」。**占位符是单字符，切块永远切不开它**：字符硬切的最小粒度就是字符，1 个字符内部没有可下刀的缝隙。多字符占位符（如 `\x00IMG{n}\x00`）会被字符硬切从中间切断，该块 `replace` 找不到完整串就静默跳过，图片描述无声消失；单字符在结构上切不开。**占位符不穿过切块管道**：`chunk_units` 切块时顺带给出每块在原文里的偏移，`assign_images` 按占位符的**起始偏移**把图挂到唯一一块（硬切级带 overlap，逐块扫会把重叠区的图挂两遍，偏移法天然只挂一块），再把该块的占位符换成 `【图片：描述】`，最后 `scrub_marks` 兜底清掉没被回填的残留（`describe_images` 的 `MAX_IMAGES` 之外没人回填）与任何 `\x00`（asyncpg 拒收含 null 字节的 str，丢的是整篇）。原图字节进 Storage 的 `knowledge` 桶，key 形如 `<source_key>/<ingest_id>/<n>.<ext>`（`source_key` 是 source_url 的 sha256 前 16 位，`ingest_id` 是本次入库的 uuid4），`documents.images` 记 chunk 级清单 `[{"key", "desc"}]`：上传失败写 `key: null`（图丢了、描述还在），**没有 `source_url` 时一条都不写**（粘贴文本/上传文件本就没有图片通道）。**重抓同一 URL 是替换不是叠加**：`ingest` 在一个事务里 `delete ... returning images` 再 insert，之后只删 returning 捕获到的那批旧 `ingest_id` 目录——版本化 key 保证旧块永远指不到新图，而「只删捕获到的那批」保证并发重抓不会互删。前端取图走 `GET /api/knowledge/images/{source_key}/{ingest_id}/{name}` 代理成 blob（`<img src>` 带不上 Authorization），路径形状与 source_key 命中已入库文档两道校验都在，扩展名白名单**不含 svg**。降级策略与长期记忆**相反**：知识库 embed 失败直接抛（没向量就检索不到，必须让调用方知道），长期记忆 embed 失败降级为 `embedding=NULL` + importance 排序。`documents` 表策略是 `using(true)`（全局共享，不按 user 隔离），但读写仍走 `rls_conn`。图片采集有三个必踩的坑：地址要先用页面 URL 补全（CDN 普遍写协议相对 `//host/a.jpg`，只认 `http(s)://` 开头会一张都采不到），请求要带浏览器 UA 与页面 Referer（图床按这两项做防盗链，缺了直接 403），svg 与白名单外的 mime 一律跳过（图标/logo 送模型只会得到噪声；svg 更是可执行的 XML，在下载那一步就拒，桶里根本不会进）。另外维基媒体有独立反爬（robot policy + TLS 指纹，curl 能过而 httpx 过不去），正文抓得到但图会被 403，属站点策略而非代码缺陷。

14. **日志**（`core/logging.py`）：`setup_logging()` 随 `import app.main` 触发，配「控制台 + 滚动文件 `logs/app.log`（10MB × 5 份）」。两处刻意的接线：① `uvicorn` / `uvicorn.error` 的 handler 清空并置 `propagate = True`，让未捕获的异常栈落进文件（uvicorn 默认只写自己的 stderr，文件里永远看不到）；`uvicorn.access` 不接，请求流水只留终端。② `httpx` / `httpcore` 提到 WARNING —— 一次建库要发几十条 embedding 请求，INFO 会把日志刷满。长文档向量化另按批次打进度（`embed_batched` 批次超 20 才打，长期记忆那种量级不打），避免几十秒黑屏。时序依赖：本函数在 uvicorn 配置完自己的日志之后才跑，靠的是 `app.main` 的 import 时机，改动启动方式时要留意。

15. **代码执行沙箱与 skill 脚本**（`docker/sandbox/` + `core/sandbox.py` + `core/files.py` + `core/skill_scripts.py` + `tools/script.py`）：**模型不写代码**——只能从 `app/skills/*/scripts/*.py` 里挑一个已评审脚本并传参（`run_skill_script(skill, script, params)`，两个名字由启动时扫描出的 `Literal` 枚举钉死，脚本源码由后端读盘，模型的手碰不到代码本身）。执行落在独立 compose 服务 `sandbox`，加固项全在 `docker/docker-compose.yml`：只读 rootfs + tmpfs `noexec`、非 root、`cap_drop: ALL`、`no-new-privileges`、内存 / `pids` / `nofile` / `fsize` 上限、**不注入任何环境变量**（黑名单式脱敏总会漏，见 Hermes CVE-2026-9368 的教训），外加双端超时（沙箱看门狗杀进程 + 后端 httpx 多等 5s 拿结构化响应）。三处**删掉不报错、只会慢慢失效**的必须记牢：**`init: true`**（不开则 PID 1 是 uvicorn，Python 当 PID 1 不 `wait()` 孤儿，脚本派生的子孙在脚本被杀后 reparent 到 PID 1 就成永不回收的僵尸，照样占 `pids_limit` 槽位——实测 fork 炸弹跑完攒 61 个僵尸、pids 64/64 打满，此后每次执行都 `BlockingIOError`，沙箱永久失效直到重启）、**`memswap_limit` 必须写成与 `mem_limit` 等值**（不写则默认是内存的 2 倍，等于白送 2g swap，实测能撑到 2.6g 才被杀）、**`pids_limit`**（内存与 CPU 拦不住 fork 炸弹，只有它拦得住）。脚本契约：stdin 读 `{"args","work_dir","out_dir"}`，文件由沙箱写进本次执行自己的 `/tmp/<uuid>/work`（**没有 `/tmp/work` 这个固定路径**），stdout 只打结论，只 import 标准库与 `docker/sandbox/pyproject.toml` 里声明过的包（标准库之外每多一个都是安全决策）。`params` 里凡是**值等于某个已知会话文件名**的字符串被认出来当引用（**只标记、不改写值**），对应字节另走 `files` 通道塞给沙箱；其余字符串原样传递。**模型见的引用是文件名不是 uuid**——`file_id` 退为纯内部标识（目录键 + 前端下载凭证），查表只认净化名、不接受 file_id 别名。代价是模型从工具结果的 `artifacts` 里照样看得见 uuid，照抄填进 `params` 会走「查表不命中 → 原样传 → 脚本 `open` 失败 → 回列本次 work_dir 里的实际文件名」这条路，而不是悄悄命中错文件。脚本用 `join(work_dir, basename(name))` 定位，**不做绝对路径分支**（那对 `/etc/passwd` 会原样返回，是纯粹的穿越入口）；路径穿越由脚本自己的 `open` 失败兜住，不需要额外校验代码。**脚本里的参数取值别复用「显示用」的截断函数**：`inspect.py` 的 `_text` 带 `SAMPLE_CHARS=32`，而 `测试数据.xlsx` 这类中文名很容易超过它，拿它处理路径会截成一个不存在的文件名。产物写回会话文件目录拿到新 `file_id`，随 `tool_result` 事件的 `artifacts` 字段下发（`run_skill_script` 必须留在 `NON_QUERY_TOOLS` 里，否则产物清单会被结构化截断）。**加脚本要重新生成 `tests/data/tool_schemas.json`**——`Literal` 枚举变了 schema 就变，快照测试会红。

16. **会话文件在 Storage + `files` 表，工具结果仍在本地**（`core/files.py` + `core/storage.py` vs `core/memory/tool_result.py`）：上传件与脚本产物的**字节在 Storage 的 `files` 桶、元数据在 `files` 表**（`FileStore.__init__(storage, bucket)`），对象 key 是 `<user_id>/<conversation_id>/<file_id>/original.<ext>`（与 `files.storage_path` 列同值，前两段让「按会话前缀删」一次到位而不读表），**活到会话结束**；spill 的 `result_dir/<user_id>/<conversation_id>/` 仍在本地磁盘、每轮结束即清（只留 `_history.txt`）。所以会话文件绝不能落进 spill 目录——下一轮 `clear_spill` 就把它删了。删除分两步且**顺序写死：先删 Storage 对象、再删会话行**（`AgentService.delete_conversation` 里 `FileStore.delete_conversation` 在前、`memory.delete_conversation` 在后）；`files` 行靠 `conversation_id` 的 `on delete cascade` 跟着会话走，前缀删一旦失败只会留下孤儿对象，反过来的话就是「永远查不到的孤儿图」。上传是 multipart（`python-multipart`）；上传时带的 `conversation_id` 若不是自己的，后端另建一个并在返回值里给出，**前端必须接管这个新 id**，否则上传件与后续对话不在同一个会话里。下载走 `GET /api/files/{file_id}?conversation_id=…`，`file_id` 必须是合法 uuid（这是防路径穿越的唯一一道校验）；前端不能用 `<a href>` 下（带不上 Bearer 会 401），得拿 blob 再另存。**按文件名反查与清单渲染共用 `FileStore.list_by_name()`**（同名取最新在这一个地方收口；`list_files` 是 `created_at` 倒序，聚合必须 `setdefault` 守卫——裸字典推导式是后写覆盖、旧件赢）。**`list_uploaded_files` 的 limit 必须 ≤ `FILE_REF_LIMIT`**（20 ≤ 50），否则出现「清单里看得见、引用时解析不到」的死角。上传入口**先净化文件名再判后缀**，判的必须是最终落盘的那个名字。对象 key 必须全 ASCII 且不含 `#`/`%`（storage-api 直接 `400 InvalidKey`，百分号编码救不了），所以扩展名经 `safe_ext` 白名单化、判不出来就丢掉。**Storage 用用户身份而非 service_role**：`StorageClient` 每次调用现签一个 180 秒的 HS256 JWT（与 `rls_conn` 注入的 claims 同构），RLS 判定发生在 storage-api 进程里——它只认签名，后端设不了它的会话状态。私有桶没有策略就是**除 service_role 外全部拒绝，包括后端自身**；`delete_prefix` 只能自己列（`DELETE /object/{bucket}` 的 `prefixes` 要的是精确对象路径列表，不是前缀）。

### 前端结构

- `views/Chat.vue`：主界面，负责 SSE 事件分发（`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`）与会话管理。**往 `messages` 里 push 完必须取回数组里的响应式代理，别留着刚构造的裸对象**（`ensureAssistant()` 里那句 `current = messages.value[len - 1]`）：流式累积全靠 `current.content += …` / `tools.push(…)` 改它，而**改裸对象不经过 Vue 的 set 代理、不触发重渲染**——数据和界面脱节，只等别处有真响应式变动（如点开气泡、`phase` 变化）时才把攒下的改动一次性刷出来，表现为「SSE 像有延迟、子任务内容点了下拉才出现」。最小复现：同一个组件里同时改「push 时的裸对象」与「`arr[i]` 取回的代理」，只有后者会让 DOM 更新。
- 记忆以弹窗形式内置于 `views/Chat.vue`：输入框旁「记忆」入口，`el-tabs` 切换长期记忆（跨会话偏好）与短期记忆（摘要 + 最近几轮对话）。
- 知识库同样是 `views/Chat.vue` 里的弹窗（`components/KnowledgeDialog.vue`，入口与「记忆」并列）：`el-radio-group` 切换粘贴文本 / 上传文件 / 抓取网址三种入库方式，另一个 tab 是已入库文档列表（按来源聚合，可整篇删除）。
- `components/ChatPanel.vue`：渲染 markdown（`marked` + `DOMPurify` 防 XSS）+ 工具气泡 + 输入框。输入区左侧的回形针是文件上传入口（`accept=".xlsx"` 只过滤选择器，真正的校验在后端），脚本产物在工具气泡下方渲染成下载卡片。
- `services/supabase.ts`：Supabase 客户端单例 + `getAccessToken()`（`getSession` 会自动刷新即将过期的 token），是前端取登录凭证的唯一出口。
- `services/sse.ts`：手写 `fetch` 流式解析 SSE（规范化换行后按 `\n\n` 分帧，处理多字节字符切断）；注入 `Authorization: Bearer`，401 抛 `AuthExpiredError` 由调用方引导重新登录。
- `services/api.ts`：axios 封装，请求拦截器异步取 token 注入 `Authorization: Bearer`。
- `stores/auth.ts`：Pinia store，GoTrue 会话 + 登录/注册/登出；`init()` 读初始会话并订阅 `onAuthStateChange`。**用户面对的是用户名登录**——GoTrue 只认 email/phone，`signIn`/`signUp` 收到的用户名在 store 内拼成 `<用户名>@my-agent.local` 再提交（`USERNAME_DOMAIN`），所以 `auth.users.email` 存的是这个假邮箱；`displayName` 反向剥掉后缀供界面展示。
- `stores/conversation.ts`：Pinia store，会话列表 + 当前会话 id。
- `router/index.ts`：`/`（需登录）+ `/login`，全局 `beforeEach` 守卫等 `auth.init()` 完成后按会话有无双向重定向。
- 历史 assistant 消息在库里存成 JSON `{"content", "tools"}`，前端 `historyToMessages` 还原为 markdown + 工具气泡。