# CLAUDE.md

写给**在本仓库改代码的 agent**。

收录判据：换台机器、换个环境依然成立，且**只读代码会做错**。只写结论与后果，不写改造史与实测过程；具体数值、文件名以代码为准。部署动作、主机地址、环境变量取值不写在这里（归 `docker/` 与 `.env`）。

## 项目概览

多业务对话式 Agent 平台，前后端分离：

- **backend/**：FastAPI + LangGraph `create_agent`，实现 ReAct → 工具调用 → critic 事实校验。高德、UAPIS、Tavily 走 HTTP；12306 与机票走 MCP；LLM 走 DashScope 的 OpenAI 兼容协议，模型名由 `LLM_MODEL` / `VISION_MODEL` / `EMBEDDING_MODEL` 配置。持久化在自托管 Supabase（Postgres + RLS + pgvector + Storage + GoTrue）。
- **frontend/**：Vue 3 + Vite + Element Plus + Pinia，登录页 + 会话列表 + 流式聊天 + 工具气泡。

## 常用命令

后端（`backend/`，依赖由 `uv` 管理）：

```sh
uv sync
uv run python run.py      # 开发服务，端口 8080
uv run pytest
uv run pytest tests/test_llm_model.py::test_get_chat_model_is_singleton
```

- 异步测试不必写 `@pytest.mark.asyncio`（`asyncio_mode = "auto"`）。
- 测试不依赖真实 LLM / 网络：假 langchain 模型注入 `service.chat_model`；数据层连真实 Postgres 测试库 `agent_test`（`SUPABASE_TEST_DATABASE_URL`），未配时相关用例 skip。
- 未配 `LLM_API_KEY` 时后端仍可启动（返回空内容的假模型）；未配 `SUPABASE_DATABASE_URL` 时启动因建池失败而报错。
- 知识库「抓取网址」需要 `uv run playwright install chromium`；Linux 缺系统库再加 `install-deps chromium`。

前端（`frontend/`）：

```sh
npm install
npm run dev          # /api 代理到 http://127.0.0.1:8080
npm run build        # vue-tsc + vite
npm run type-check
```

## 架构

```
路由层                    service 编排层                    核心执行层
api/routes/chat.py  →  services/agent_service.py  →  core/agent.py (ReAct 循环)
  POST /api/agent/chat    AgentService.chat_stream         Agent.run_stream
  (SSE)                   装配历史/记忆/LLM/工具/技能      → core/registry.py (工具元数据)
                          + 知识库检索 + critic + LTM 提炼   → core/llm.py (模型工厂)
```

- `core/agent.py`：`Agent.run_stream` 实现 ReAct 循环。工具由框架的 `ToolNode` 执行（参数校验、并行调度、异常与 `Command` 控制流都归它）；`_ToolExecMiddleware` 只做事件上报、结构化截断与 spill。轮次上限由 `_ToolRoundLimitMiddleware` 按 `settings.max_tool_rounds` 计数（一轮 = 一次带 `tool_calls` 的模型响应），到点后摘掉工具强制作答；图额度按 `max_tool_rounds` 的 10 倍派生，只当硬保护。
- `core/registry.py`：`ToolRegistry` 管理工具元信息（工具对象 + 中文 label）；模型侧 schema 由各 tool 的 pydantic `args_schema` 直接转换。返回 dict/list 的工具用 `@tool(response_format="content_and_artifact")` 返回 `(content, raw)`：content 给模型，artifact 给截断/spill/前端侧信道；纯文本工具保持普通 `@tool`。
- `core/llm.py`：全项目唯一 LLM 通路。`get_chat_model()` 单例（DashScope 走 OpenAI 兼容协议，故用 `ChatOpenAI`）；`close_chat_model()` 由 lifespan 调用。

## 不变量

### 1. critic 事实校验

仅当本轮调用了硬数据工具（`HARD_DATA_TOOLS`：车票 / 机票 / 天气 / 节假日）才触发。主 agent 这轮若只在委派（`call_sub_agent`），`hard_results` 为空，critic 不会触发——子 Agent 必须自己带 critic。

回路挂在 `create_agent` 的收口钩子 `aafter_agent` 上。收口链按「middleware 列表末尾 → 开头」执行，critic 必须挂在末尾才能最先跑；测试有护栏钉住这个位置。

ReAct 轮答案先缓存、不下发；critic 不通过时**复用已查到的工具结果**让 LLM 直接重写（不重新调工具、不走 ReAct），改完再校验。**是环不是一次性**，最多改写 `CriticConfig.max_rewrite` 次。最终答案先落库、再拆块流式下发——`token` 事件是校验通过后的分块补发，不是逐 token 流式。

落库与生成跑在 `chat_stream` 的独立 worker task 里，主协程只转发 SSE。客户端断开只取消转发，worker 照常跑完并落库。

critic 是多轮带工具的校验：可用 `read_file` / `grep` 读回完整落库结果做事实比对，限 `max_iter` 且 fail-open；校验语料与模型生成时所见一致（截断版 `result`）。

### 2. 工具只在启动时装配一次

`AgentService.__init__` 调 `_build_registry()` 一次性创建所有数据源并缓存到 `self.registry`；进程退出时 `service.close()` 统一关闭。**新增工具保持这个模式**：每请求重建会让 httpx 连接 / MCP session 在进程内不断累积。

### 3. 用户隔离 = 登录 + RLS

前端用 `supabase-js` 直连 GoTrue，拿到 JWT 后以 `Authorization: Bearer` 调后端。后端 `core/auth.py` 用 `SUPABASE_JWT_SECRET` 本地 HS256 验签，取 `sub` 当 `user_id`；缺失或失效一律 401，没有匿名兜底。

数据侧是双保险：业务方法显式带 `user_id`，且每条查询都跑在 `core/db.py` 的 `rls_conn(user_id)` 事务里（`set_config('role','authenticated')` + 注入 `request.jwt.claims`）。**`postgres` / `service_role` 是 `BYPASSRLS` 角色**，直连裸池跑查询策略完全不执行；裸池 `admin_conn` 只给测试基础设施用。

storage 链路被拒（后端现签的 HS256 JWT 被 storage-api 拒）是「有人动了 auth 密钥」的第一信号。**不要启用 Supabase 非对称密钥**：会把 JWT 切成 ES256，同时打断后端验签与 Storage。

### 4. SSE 协议

`EventSourceResponse(gen(), sep="\n")`，`sep="\n"` 是刻意的——默认 `\r\n` 会让前端按 `\n\n` 分帧永远匹配不到。

事件类型：`conversation_id` / `token` / `tool_call` / `tool_result` / `status` / `done`。`tool_call` 与 `tool_result` 都携带 `id`（tool_call_id）：同名工具并行且乱序完成时，前后端按 `id` 配对，不能按工具名或队列顺序。

### 5. skill 两段式

`app/skills/<name>/SKILL.md` 的 YAML frontmatter（`name` + `description`）常驻 system prompt 的 `{skill_directory}` 插槽；正文由 `get_skill` 工具按需加载（`name` 用 enum 约束）。新增业务 = 加一个目录 + `SKILL.md`，无需改装配代码。

`## 输出格式` 用 Template pattern：把结构包进 markdown 代码块，块内**只放字面输出与 `{占位符}`**，一句说明都不要放——写进块里的说明会被模型当内容照抄。块的严格度和多套方案的写法（哪几节每套各写一遍、哪几节全篇一份）写在块外。**规则正文里让模型「列出」的东西，模板里必须有对应小节**，否则模型只能自由发挥。

### 6. 提示词集中在 `app/prompts/*.md`

`system.md`（插槽 `{skill_directory}` / `{tool_grounding}` / `{today}` / `{long_term_hint}`，由 `render_system_prompt` 用 `str.replace` 填充）、`grounding.md`（严禁编造事实数据）、`critic.md`（critic 输出 JSON 契约）、`vision.md`（图片描述与「有无有效信息」判定，含 `{title}` 插槽）。`today_hint()` 注入当前日期，避免模型编造过去日期。

### 7. 数据源分层

`McpDataSource` 封装 MCP 的「懒加载持久 session + 超时 + 失效重建」（12306 / 机票）；`HttpDataSource` 封装 httpx 异步客户端（高德 / UAPIS / Tavily）。`amap_web.py` 把高德返回解析成约定结构，纯函数（`parse_poi_list` / `parse_weather`）便于单测。

### 8. 配置

`app/config.py` 用 pydantic-settings 从 `backend/.env` 读取。`EMBEDDING_API_KEY` / `VISION_API_KEY` / `VISION_MODEL` 必须显式配，**不做复用兜底**——兜底会让「没配」和「配错了」长得一样。`EMBEDDING_DIMENSIONS` 必须与建表的 `vector(N)` 一致，换 embedding 模型时两处一起改。密钥不写进 compose。前端 `VITE_` 变量在构建时内联，改了要重新构建。

### 9. 子 Agent 委派

顶层 agent 用 `call_sub_agent` 把整块独立工作委派给独立上下文的子 Agent：子 Agent 历史为空、只吃 `task`，业务规则由它按需 `get_skill` 加载，跑完只回传最终答案。

**子 Agent 必须自己带 critic**：主 agent 这轮调的是 `call_sub_agent`、不在 `HARD_DATA_TOOLS` 里，主 agent 的 critic 不会触发；service 装配时把同一个 `CriticConfig` 传进去。

**触发判据写在各业务 `SKILL.md` 的步骤里，不写 system.md / grounding.md**（后两处每轮恒可见，但不该承担业务决策）。认的场景只有两类：用户要 2-3 套方案、或一次规划多个目的地；且「子工作 ≥3-4 轮工具调用 / 中间结果父 agent 不要原始形态 / 子工作之间不共享约束」三条同时满足。

**扇出粒度由业务的全局约束决定**：travel 可按目的地或按套；`meal_planning` 只能按套，**绝不能按天**——「荤菜素菜全程不重样」是跨天约束，按天拆开各子任务互相看不见对方选了什么，必然重样。

「委派前先自己查共享数据」只在该业务输出模板真的需要那些字段时才加：travel 需要天气 / 交通，meal_planning 不需要。照搬会给模型错误暗示。

`call_sub_agent` 的 `title` 是**必填参数**，用来区分并行委派节点；不能拿 `task` 截断当名字。子 Agent 的摘要是 LLM 生成的无界散文，不在 `NON_QUERY_TOOLS` 里，超 `truncate_limit` 照常走 spill。

当前是单层委派（子 Agent 工具集不含 `call_sub_agent`）。子 Agent 有独立 `sub_conversation_id` 隔离 spill 目录，任务结束（含异常）即清理。

**进度按层级上报**：`current_emit` / `current_tool_call_id` 两个 contextvar 由 `run_stream` 与 `_ToolExecMiddleware` 注入 / 复位；子 Agent 转发事件前盖两个归属章——`agent`（`assistant` / `sub`）与 `parent`（发起它的 `call_sub_agent` 的 tool_call_id）。并行工具各在自己的 Task 里有独立 context 副本，不会互相认领。service 原样透传并随 `tools` 落库，前端按 `parent` 挂到对应委派节点。

### 10. 三层记忆

- **工作记忆**：本轮工具结果，内存，每次问答即丢。
- **短期记忆**：`short_term_memory` 表，喂 LLM 的跨轮上下文**权威源**（1 条 summary + 结构化消息）。`messages` / `conversations` 仅前端展示，不参与 LLM 上下文。
- **长期记忆**：`long_term_memory` 表，按 `user_id` 跨会话；每条 fact 入库时向量化。

编排统一在 `memory/manager.py` 的 `MemoryManager`，四个 store 由 `AgentService` 创建后共享。**四个 store 的 `__init__` 一律零 IO**：进程级单例在 import 时就构造 store，而连接池要到 lifespan 才建。四张表的连接统一走 `core/db.py`。

token 预算用 `tiktoken`（cl100k_base）真实计数，失败回退启发式。短期记忆超预算时，早期回合压成累积摘要（存表 + 落盘 `_history.txt`）。长期记忆 fire-and-forget 只提炼本轮 user 消息（per-user 锁串行），召回按本轮问题语义检索，向量链路失败回退 importance；写入前与已有 fact 做语义去重（余弦 ≥ 0.85 视为同一条，提升 importance 而非新增）。工作记忆单个结果超长时 spill；多结果累积超预算时成对淘汰最老 + LLM 蒸馏兜底（蒸馏失败降级为拼接截断，不炸整轮）。

命名：工作 / 短期 / 长期记忆用 `working` / `short_term` / `long_term`，会话历史用 `conversation`，禁止 `session` 等混用。

**两个 LLM 蒸馏点**（`manager.py` 的 `_update_summary`、`working.py` 的 `_distill`）的提示词都显式要求文件名原样保留——文件名是重跑脚本恢复产物的唯一指针；两处都有护栏测试。

### 11. 工具结果地址索引（spill）

结果超 `truncate_limit` 时，完整结果写 `result_dir/<user_id>/<conversation_id>/<uuid>.txt`，窗口只放「预览 + 文件路径」。模型用写死的 `read_file(path, offset, limit)` 与 `grep(pattern, path)` 读回；path 经白名单校验（拒绝 `..` 与绝对路径），按 `current_user_id` 隔离目录。

生命周期：spill 文件在本轮的 `finally` 里清理（保留 `_history.txt`），成功与异常都清；清理失败只告警，不盖原始异常。`delete_conversation` 删整个会话目录；子 Agent 结束即清自己的目录。会话文件**不能**落进 spill 目录——下一轮就被清了。

### 12. 短期记忆组装与压缩

`append_turn` 每轮写入：user + 每轮 assistant(tool_calls) + assistant 答案。**tool 结果与 tool 占位消息都不落表**，组装时 `_expand_record` 动态生成空串占位配对。

组装预算 = `llm_context_budget * history_budget_ratio`。超预算时早期回合序列化 + 蒸馏 + 追加 `_history.txt`，并**物理删除**早期消息（删除即增量，没有 `compressed_through` marker）；最近几轮保留结构化消息。摘要作为独立 system 消息注入，包含完整历史文件路径，模型可 `read_file` 读回。

拼上下文 = system（主提示词 + 摘要消息）+ 最近几轮结构化消息 + 本轮工作记忆。这样模型跨轮能看到「查过哪些工具、什么参数」；没查过的对象仍按 grounding 每轮重新调工具。

### 13. 本地知识库（RAG）

`documents` 表存切块语料（`source_url` 是文档唯一标识），写入用 `text-embedding-v4` 向量化，检索走 `match_documents` 余弦 top-k，**再过一道 `SIMILARITY_FLOOR` 相关性下限**。那条 SQL 是纯 top-k、没有门槛，库里只要有一行就必返回 `match_count` 条；没有下限时「知识库暂无相关内容」永远不可达，无关片段会被当权威资料写进答案。

知识库记的是**用户自己收录的资料**，含 POI 接口给不出的细节，所以优先级高于通用查询工具。这条规则必须写在 `prompts/grounding.md`（永远可见），不能只写 SKILL.md——短问题不会触发 `get_skill`，正文压根不加载。

建库只在「知识库弹窗」触发，对话时一律不爬。URL 走 Playwright（跑完 JS 再取 DOM）+ trafilatura 提取正文（markdown，保留表格 / 图片 / 链接，关评论区；不覆盖 `MIN_EXTRACTED_SIZE`）。入库前还有 `_MIN_CONTENT_LEN` 拦截：抓不到时同样会产出「内容」，限流页的错误体会变成知识库噪声。

图片位置留**单个私用区字符**占位符。多字符占位符会被字符硬切从中间切断，`replace` 找不到完整串就静默跳过，图片描述无声消失；单字符在结构上切不开。图片描述由 `VISION_MODEL` 生成；**判定必须带页面标题做上下文**，否则同一张图的判定会摇摆。判据要客观（画面里有可辨认内容就描述），不能写「对行程规划有用」——菜谱页 / 教程页的图会被误判掉。

`chunk_units` 切块时给出每块偏移，`assign_images` 按占位符**起始偏移**挂到唯一一块（硬切带 overlap，逐块扫会挂两遍），再把占位符换成 `【图片：描述】`；`scrub_marks` 兜底清残留与 `\x00`（asyncpg 拒收 null 字节，丢的是整篇）。

原图进 Storage 的 `knowledge` 桶，key 形如 `<source_key>/<ingest_id>/<n>.<ext>`；`documents.images` 记 chunk 级清单 `[{"key","desc"}]`，上传失败写 `key: null`，没有 `source_url` 时一条都不写。**重抓同一 URL 是替换不是叠加**：一个事务里 `delete ... returning images` 再 insert，之后只删返回捕获到的旧 `ingest_id` 目录；版本化 key 保证旧块指不到新图，只删捕获到的那批保证并发重抓不互删。

前端取图走 `GET /api/knowledge/images/...` 代理成 blob（`<img src>` 带不上 Authorization），路径形状与 source_key 命中已入库文档两道校验，扩展名白名单**不含 svg**。

降级策略与长期记忆**相反**：知识库 embed 失败直接抛（没向量就检索不到），长期记忆失败降级为 `embedding=NULL` + importance 排序。`documents` 策略是 `using(true)` 全局共享，但读写仍走 `rls_conn`。

图片采集的坑：地址要先按页面 URL 补全（CDN 常写协议相对 `//host/a.jpg`）；请求带浏览器 UA 与 Referer（图床防盗链）；svg 与白名单外 mime 一律跳过（svg 是可执行 XML，在下载那一步就拒）。

### 14. 日志

`setup_logging()` 随 `import app.main` 触发，配「控制台 + 滚动文件 `logs/app.log`」。两处刻意接线：`uvicorn` / `uvicorn.error` 的 handler 清空并 `propagate = True`（让未捕获异常栈落进文件）；`uvicorn.access` 不接（请求流水只留终端）。`httpx` / `httpcore` 提到 WARNING（一次建库几十条 embedding 请求会刷屏）。长文档向量化按批次数打进度。时序上依赖 `app.main` 的 import 晚于 uvicorn 配置日志，改启动方式时要留意。

### 15. 代码执行沙箱与 skill 脚本

**模型不写代码**：只能从 `app/skills/*/scripts/*.py` 里挑一个已评审脚本并传参（`run_skill_script(skill, script, params)`，两个名字由启动时扫描出的 `Literal` 枚举钉死，源码由后端读盘）。

加固项全在 `docker/docker-compose.yml`，三处删掉不报错、只会慢慢失效：`init: true`（否则孤儿进程成僵尸，占满 `pids_limit` 后沙箱永久失效）、`memswap_limit` 必须等于 `mem_limit`（不写默认给 2 倍 swap）、`pids_limit`（内存 / CPU 拦不住 fork 炸弹）。另有只读 rootfs + tmpfs `noexec`、非 root、`cap_drop: ALL`、`no-new-privileges`、内存 / 文件大小上限、**不注入任何环境变量**（黑名单式脱敏总会漏）。超时是双端：沙箱看门狗杀进程 + 后端 httpx 多等 5s。

脚本契约：stdin 读 `{"args","work_dir","out_dir"}`，文件在本次执行自己的 `/tmp/<uuid>/work`（没有固定 `/tmp/work`），stdout 只打结论，只 import 标准库与 `docker/sandbox/pyproject.toml` 里声明过的包。

`params` 里凡是值等于某个已知会话文件名的字符串被认出来当引用（只标记、不改写值），字节另走 `files` 通道。**模型见的是文件名不是 uuid**：`file_id` 退为内部标识，查表只认净化名；把 uuid 照抄进 `params` 会走「查表不命中 → 原样传 → 脚本 open 失败」，不会悄悄命中错文件。脚本用 `join(work_dir, basename(name))` 定位，不做绝对路径分支（那是纯粹的穿越入口）。脚本参数不要复用「显示用」的截断函数（如 `inspect.py` 的 `_text`），否则中文文件名会被截成不存在的名字。

产物写回会话文件目录拿新 `file_id`，随 `tool_result` 的 `artifacts` 下发（`run_skill_script` 必须留在 `NON_QUERY_TOOLS`）。**加脚本要重新生成 `tests/data/tool_schemas.json`**：`Literal` 枚举变了 schema 就变，快照测试会红。

### 16. 会话文件与 Storage

上传件与脚本产物的**字节在 Storage 的 `files` 桶、元数据在 `files` 表**，对象 key 是 `<user_id>/<conversation_id>/<file_id>/original.<ext>`（与 `files.storage_path` 同值），活到会话结束。spill 仍在本地、每轮清。

删除顺序写死：**先删 Storage 对象、再删会话行**。反过来一旦前缀删改成「读表再删」，就会留下永远查不到的孤儿对象。

上传是 multipart；带的 `conversation_id` 若不是自己的，后端另建一个并在返回值里给出，**前端必须接管新 id**。下载走 `GET /api/files/{file_id}?conversation_id=…`，`file_id` 必须是合法 uuid（防路径穿越的唯一校验）；前端不能用 `<a href>`（带不上 Bearer），要拿 blob 再另存。

`list_by_name()` 收口「同名取最新」（SQL `distinct on`），清单渲染与参数解析共用它。`list_uploaded_files` 的 limit 必须 ≤ `FILE_REF_LIMIT`，否则「清单里看得见、解析时找不到」。

上传先净化文件名再判后缀，判的是最终落盘名。对象 key 必须全 ASCII 且不含 `#` / `%`（storage-api 直接 `400 InvalidKey`），扩展名经 `safe_ext` 白名单化。

**Storage 用用户身份而非 service_role**：`StorageClient` 每次调用现签短时 HS256 JWT，RLS 判定在 storage-api 进程里。私有桶没有策略就是除 service_role 外全部拒绝——包括后端自己。`delete_prefix` 只能自己 list 再按精确路径批量删（批量删端点不做前缀匹配，传目录路径返回 200 且一个都不删）。

### 前端约定

- `views/Chat.vue` 往 `messages` 里 push 完必须取回数组里的响应式代理再改（`current = messages.value[len - 1]`）。改裸对象不触发重渲染，表现为「SSE 像有延迟、子任务内容点了下拉才出现」。
- `services/sse.ts` 按 `\n\n` 分帧、处理多字节字符切断；401 抛 `AuthExpiredError`。
- 用户名登录：`stores/auth.ts` 把用户名拼成 `<用户名>@my-agent.local` 提交给 GoTrue，展示时剥掉后缀。
