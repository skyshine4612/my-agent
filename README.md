# 个人智能助手

多业务对话式 Agent 平台。后端基于 FastAPI + LangGraph 实现「通用 agent → 工具调用 → critic 事实校验」链路，前端是 Vue 3 通用对话界面。内置三个业务 skill（旅行规划、饮食规划、Excel 字段脱敏），工具覆盖 POI/推荐菜、天气、节假日、翻译、热榜、菜谱、营养、联网搜索，另有一个本地知识库（RAG）存放自己收录的攻略语料；新增业务按「skill + 工具」扩展。

## 功能特性

- **ReAct 推理-行动循环**：基于 LangGraph 的 `create_agent` 图，LLM 自主决策调用工具、并行执行、回填结果、再决策。
- **事实校验回路（critic）**：调用硬数据工具（车票/机票/天气/节假日）后，多轮带工具自动比对回答与完整工具结果，发现编造事实时基于真实结果重写答案。
- **真实数据源**：高德（POI/推荐菜，Web 服务 API）、天气/节假日/翻译/热榜/菜谱/营养（UAPIS，天气最多 7 天预报）、搜索（Tavily）经 HTTP 直连；12306 火车票经 ModelScope 托管 MCP；机票直连 variflight 官方 MCP。
- **标准 skill 两段式**：业务规则（`SKILL.md`）的清单常驻 system prompt，正文按需加载，新增业务无需改装配代码。
- **子 Agent 委派**：复杂子任务可委派给独立上下文的子 Agent 处理、只返回摘要，避免主上下文被大量工具结果淹没。
- **三层记忆**：工作记忆（本轮工具结果，易失）+ 短期记忆（summary 摘要 + 最近几轮结构化对话含工具调用，独立表持久化，是喂 LLM 的权威源）+ 长期记忆（跨会话偏好），记忆与会话历史分离：长期/短期记忆各从各表读，messages/conversations 仅前端展示；短期记忆超预算压成摘要（物理删除早期回合），工作记忆超长 spill（摘要 + 文件地址索引），子 Agent 按子会话隔离工具结果地址索引。
- **本地知识库（RAG）**：自己收录的攻略语料（粘贴文本 / 上传文件 / 抓取网址三种入库方式）切块后用 `text-embedding-v4` 向量化存进 pgvector，检索按余弦相似度取 top-k，再过一道相关性下限（低于阈值直接视为「知识库暂无相关内容」，避免把无关片段当权威资料）。攻略页插图先转成文字描述再入库（知识库是文本语义检索，图片不转成文字就搜不到），**原图同时存进 Supabase Storage**，检索命中时随工具结果一起下发，对话里直接渲染成缩略图、点击可放大。知识库优先于通用查询工具：它记的是 POI 接口给不出的细节（具体店名、营业时间、避坑经验）。
- **长结果地址索引**：工具结果超阈值时，完整结果写临时文件，窗口只留预览与路径，模型用 `read_file`/`grep` 按需读回；本轮结束清理，历史答案另存 `_history.txt` 供跨轮读回。
- **记忆查看**：输入框旁「记忆」入口，弹窗内 tab 切换长期偏好与当前会话的短期记忆（摘要 + 最近几轮对话）。
- **SSE 流式对话**：答案校验通过后按块流式下发（不是逐 token），实时展示工具调用进度；完整回复在流式输出前已落库，刷新后自动恢复会话与历史。
- **账号登录 + 用户隔离**：Supabase Auth（GoTrue）注册/登录，前端持 JWT 调后端；后端本地 HS256 验签取用户身份，数据层按 `user_id` 过滤 **且** 每条查询都在 `role=authenticated` 事务里跑，由 Postgres RLS 策略兜底。

## 技术栈

| 层 | 技术                                                                                    |
|---|-----------------------------------------------------------------------------------------|
| 后端 | Python 3.11+ · FastAPI · LangGraph · langchain · langchain-openai · pydantic-settings · openai SDK（embedding / vision）· mcp · sse-starlette · asyncpg · PyJWT · tiktoken |
| 前端 | Vue 3 · Vite · TypeScript · Element Plus · Pinia · supabase-js · marked + DOMPurify      |
| 存储 / 身份 | 自托管 Supabase（Postgres + pgvector + Storage + GoTrue），6 张业务表全开 RLS          |
| 抓取 | Playwright（跑完 JS 再取 DOM）+ trafilatura（提取正文）                                    |
| LLM | 阿里云百炼 DashScope（OpenAI 兼容协议，模型名由配置决定）                                |
| 基础设施 | Docker + docker-compose（`docker/`：Supabase 全套 + 代码执行沙箱）                       |

## 目录结构

```
my-agent/
├── backend/
│   ├── app/
│   │   ├── api/routes/        # HTTP 入口：SSE 对话、会话、记忆、知识库、文件
│   │   ├── core/              # ReAct 图、critic、LLM、RLS 连接、沙箱
│   │   │   ├── knowledge/     # 切块 / 向量化 / 抓取入库 / 检索 / 图片描述
│   │   │   ├── memory/        # 工作记忆 / 短期记忆 / 长期记忆
│   │   │   └── storage/       # Storage 客户端 + 会话文件
│   │   ├── datasource/        # MCP（12306 / 机票）+ HTTP（高德 / UAPIS / Tavily）
│   │   ├── models/            # Pydantic 请求模型
│   │   ├── prompts/           # 运行时提示词
│   │   ├── services/          # AgentService 编排器
│   │   ├── skills/            # 业务 skill（travel / meal_planning / excel）
│   │   ├── tools/             # 工具定义与注册
│   │   ├── config.py          # 配置（读取 .env）
│   │   └── main.py            # FastAPI 应用入口
│   ├── tests/                 # pytest 测试（不依赖真实 LLM / 网络）
│   ├── pyproject.toml         # 依赖 + pytest 配置
│   └── run.py                 # 开发启动入口
├── frontend/
│   └── src/                   # views / components / services / stores / router / types
├── deploy/                    # schema.sql（业务表 + RLS + 检索函数）、storage.sql（桶策略）
├── docker/                    # 自托管 Supabase 栈 + sandbox 服务
├── CLAUDE.md                  # 给改代码的 agent：命令、架构、不变量
├── DATABASE.md                # 表结构、RLS、Storage 与 spill 落盘约定
└── README.md
```

## 快速开始

### 前置：自托管 Supabase + 沙箱

存储、登录与代码执行沙箱都由仓库自带的 `docker/docker-compose.yml` 起：

```bash
cd docker
cp env.example .env     # 填 POSTGRES_PASSWORD / JWT_SECRET，并重新生成 ANON_KEY / SERVICE_ROLE_KEY
docker compose up -d    # 起 Supabase 全套 + sandbox，API 网关默认 8000 端口
```

`env.example` 里的 `ANON_KEY` / `SERVICE_ROLE_KEY` 是官方 demo 值，必须用你的 `JWT_SECRET` 重新签发（[官方 generate-keys.sh](https://github.com/supabase/supabase/blob/master/docker/utils/generate-keys.sh)），否则 GoTrue 签发的 token 与后端验签、Storage 用的密钥对不上。其余变量含义见[官方自托管指引](https://supabase.com/docs/guides/self-hosting/docker)。

拿到 `docker/.env` 里的 `JWT_SECRET` 与 `ANON_KEY`，然后在 Studio（网关 `/`，默认 8000 端口）里建库：

- **主库 `postgres`**：跑 `deploy/schema.sql`——建 `extensions` schema 与 `vector` 扩展（`documents` 与 `long_term_memory` 的向量列依赖它，这里是幂等兜底），再建 6 张业务表（`conversations` / `messages` / `short_term_memory` / `long_term_memory` / `documents` / `files`）+ 各自的 RLS 策略 + 两个向量检索函数（`match_documents` / `match_long_term`）。表用 `if not exists`、策略先 drop 再建、函数 `create or replace`，可反复执行：

  ```sh
  psql "$SUPABASE_DATABASE_URL" -f deploy/schema.sql
  ```

  > 重跑**不会给老库加列**（`create table if not exists` 对已存在的表不生效），加了列的版本要删表重建。

- **测试库 `agent_test`**：同一个 Postgres 实例上的**另一个 database**，与 `postgres` 并列（Supabase Studio 只连 `postgres`、没有选库入口，所以那里看不到它；`\l` 或 `select datname from pg_database` 能列出来）。

  **库本身要一次性手工建**（`createdb agent_test`，或用 Studio 对 `postgres` 库执行 `create database agent_test`）；`backend/tests/conftest.py` 只负责在里面建结构——session 级 fixture 连上后跑 `AUTH_STUB_SQL`（造 `auth.users` 与 `auth.uid()` 桩）与 `BUSINESS_SQL`（主库那份结构），再逐用例清表。库不存在时 pytest 会在该 fixture 直接 `InvalidCatalogNameError` 报错，不是 skip。

再建两个**私有**桶 `files` 与 `knowledge`，然后跑 `deploy/storage.sql` 建 `storage.objects` 上的策略。**这一步必须等 Supabase 起过一次之后再做**——`storage` schema 与 `storage.foldername()` 由 storage-api 首次启动时自建，在那之前这个文件跑不了。

### 本地开发

**1. 配置后端**

```bash
cd backend
cp .env.example .env   # 填入真实密钥（见下方配置说明）
```

**2. 启动后端**（需 Python 3.11+）

```bash
cd backend
uv sync                 # 安装依赖
uv run python run.py    # 启动于 http://0.0.0.0:8080（reload）
```

> 未配 `SUPABASE_DATABASE_URL` 时启动会因连接池建不起来而报错；未配置 `LLM_API_KEY` 时后端仍可启动，但对话返回空内容。

**3. 安装无头浏览器**（仅知识库「抓取网址」需要）

`uv sync` 只装 Python 依赖，**不会**下载 Chromium——抓网址入口靠真实浏览器跑完 JS 再取 DOM，首次必须单独装（约 700MB）：

```bash
cd backend
uv run playwright install chromium
```

Linux 上若报缺少系统库，再执行 `uv run playwright install-deps chromium`（需要 root）。未装时粘贴文本 / 上传文件入口照常可用，只有抓网址会失败，并在前端提示这条安装命令。

**4. 配置并启动前端**（需 Node 22+）

```bash
cd frontend
cp .env.example .env    # 填 VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY
npm install
npm run dev             # 启动于 http://127.0.0.1:5173，/api 代理到 8080
```

浏览器访问前端地址，注册账号后即可对话。

> `VITE_` 前缀的变量在构建时被内联进产物，改完 `.env` 需重启 dev server / 重新 build。

### 部署

仓库只提供基础设施容器（`docker/`），后端与前端按上面的「本地开发」步骤在宿主机启动：

- Supabase 与 `sandbox` 由 `docker compose up -d` 起；后端通过 `SUPABASE_URL` / `SANDBOX_URL` 指向它们——后端与容器同机时填服务名，跨机时填宿主机地址与映射端口（`sandbox` 映射 `8001`）。
- 前端 `npm run build` 的产物交给静态服务器托管，`/api` 反代到后端 `:8080`；构建前先备好 `frontend/.env`（`VITE_` 变量在构建时内联）。
- 部署后端的那台机器同样要先装 Chromium（见「本地开发 → 3. 安装无头浏览器」）；后端若跑在容器里，需在镜像构建时装进浏览器，或通过 `PLAYWRIGHT_BROWSERS_PATH` 挂载宿主机的浏览器缓存，否则知识库抓网址会失败。

## 配置说明

`backend/.env` 支持的配置项：

| 变量 | 说明 | 默认值                                              |
|---|---|-----------------------------------------------------|
| `LLM_API_KEY` | LLM 服务 API Key | 空                                                  |
| `LLM_BASE_URL` | LLM 接口地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | 使用的模型 | 见 `.env.example` |
| `ENABLE_THINKING` | Qwen 思考模式开关（`false` 关闭更快，`true` 更强推理但更慢） | `false`                                            |
| `LLM_TEMPERATURE` | LLM 采样温度 | `0.5`                                                |
| `EMBEDDING_API_KEY` | 向量化 API Key（知识库与长期记忆共用）。值通常与 `LLM_API_KEY` 相同，但必须显式配 | 空 |
| `EMBEDDING_MODEL` | 向量化模型 | `text-embedding-v4`                                 |
| `EMBEDDING_DIMENSIONS` | 向量维度，**必须与建表的 `vector(N)` 一致** | `1024`                                              |
| `VISION_MODEL` | 多模态模型（攻略页插图转文字描述），需支持图片输入 | 空（必须显式配） |
| `VISION_API_KEY` | 多模态 API Key。值通常与 `LLM_API_KEY` 相同，但必须显式配 | 空 |
| `MODELSCOPE_TOKEN` | ModelScope 访问令牌（调用 MCP 服务） | 空                                                  |
| `TRAIN_12306_URL` | 12306 火车票 MCP 服务地址 | 空                                                  |
| `FLIGHT_VARIFLIGHT_URL` | variflight 官方 MCP 服务地址 | 空                                                  |
| `VARIFLIGHT_API_KEY` | variflight 官方 API Key（X-API-Key 认证） | 空                                                  |
| `UAPIS_API_KEY` | UAPIS 令牌（节假日/翻译/热榜/菜谱/营养；天气接口免费无需 key） | 空                                                  |
| `TAVILY_API_KEY` | Tavily API Key（搜索/网页提取） | 空                                                  |
| `AMAP_API_KEY` | 高德 Web 服务 Key（POI/推荐菜） | 空                                                  |
| `SUPABASE_DATABASE_URL` | 主库 `postgres` 的连接串（`postgresql://postgres:...@<host>:5432/postgres`） | 空 |
| `SUPABASE_TEST_DATABASE_URL` | 测试库 `agent_test` 的连接串，未配置时跳过需要真实 Postgres 的用例 | 空 |
| `SUPABASE_JWT_SECRET` | 与 Supabase `docker/.env` 的 `JWT_SECRET` 完全一致，本地 HS256 验签用 | 空 |
| `SUPABASE_URL` | Supabase 网关地址（后端访问 Storage 用），不是数据库连接串 | 空 |
| `LLM_CONTEXT_BUDGET` | 工作记忆 token 预算上限（超限淘汰最老 + LLM 摘要兜底） | `128000`                                            |
| `TRUNCATE_LIMIT` | 工具结果字符串截断阈值（超限完整结果写文件，read_file/grep 按需读） | `4000`                                              |
| `RESULT_DIR` | 工具结果地址索引的落盘目录（本轮用完即删） | `results`                                           |
| `HISTORY_BUDGET_RATIO` | 历史答案原文占 `llm_context_budget` 的比例（早期答案落盘+摘要） | `0.5`                                                |
| `LONG_TERM_HINT_RATIO` | 长期记忆 hint 占 `llm_context_budget` 的比例（召回偏好注入 system） | `0.05`                                              |
| `SANDBOX_URL` | 代码执行沙箱地址（容器内用服务名，本地指向映射端口） | `http://sandbox:8001`                               |
| `SANDBOX_TIMEOUT` | 沙箱执行超时秒数（后端实际多等 5s 拿结构化响应） | `60`                                                |

### 密钥获取来源

| 变量 | 获取网站 / 路径 |
|---|---|
| `LLM_API_KEY` | [阿里云百炼控制台](https://bailian.console.aliyun.com) → API-KEY 管理 |
| `MODELSCOPE_TOKEN` | [ModelScope 魔搭社区](https://modelscope.cn) → 个人中心 → 访问令牌（Access Token） |
| `VARIFLIGHT_API_KEY` | [飞常准飞友 AI 开放平台](https://ai.variflight.com) → 注册后「API Keys」页（[ai.variflight.com/keys](https://ai.variflight.com/keys)） |
| `UAPIS_API_KEY` | [UAPIS 开放接口平台](https://uapis.cn) → 注册后控制台（[uapis.cn/console](https://uapis.cn/console)）「API keys」标签页 |
| `TAVILY_API_KEY` | [Tavily](https://app.tavily.com) → 注册登录后「API Keys」页 |
| `AMAP_API_KEY` | [高德开放平台](https://lbs.amap.com) → 控制台 → 应用管理 → 创建应用添加 Key（Web 服务） |
| `SUPABASE_DATABASE_URL` / `SUPABASE_JWT_SECRET` | 自托管 Supabase 的 `docker/.env`（`POSTGRES_PASSWORD` / `JWT_SECRET`）拼出，端口默认 `5432` |

`frontend/.env` 支持的配置项：

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VITE_SUPABASE_URL` | 浏览器直连 Supabase 的地址（API 网关，自托管默认 `http://localhost:8000`） | 空 |
| `VITE_SUPABASE_ANON_KEY` | Supabase 匿名公钥，取自 `docker/.env` 的 `ANON_KEY`（**不是** `SERVICE_ROLE_KEY`） | 空 |
| `VITE_AMAP_WEB_JS_KEY` | 高德 Web 端 JS Key（地图组件用） | 空 |

## API 概览

所有接口以 `/api` 为前缀，通过请求头 `Authorization: Bearer <access_token>` 识别用户（后端本地 HS256 验签取 `sub`，缺失或失效一律 401）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/chat` | SSE 流式对话（事件：`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`） |
| `GET` | `/api/conversations` | 列出当前用户会话 |
| `POST` | `/api/conversations` | 新建会话 |
| `GET` | `/api/conversations/{id}` | 获取会话历史消息 |
| `DELETE` | `/api/conversations/{id}` | 删除会话 |
| `POST` | `/api/files` | 上传会话文件（multipart；`conversation_id` 不属于当前用户时另建会话并在返回值里给出） |
| `GET` | `/api/files/{file_id}?conversation_id=…` | 下载会话文件 |
| `GET` | `/api/memory/long-term` | 当前用户长期记忆（跨会话偏好） |
| `GET` | `/api/memory/short-term/{conversation_id}` | 某会话短期记忆（summary 摘要 + 最近几轮对话） |
| `POST` | `/api/knowledge/text` | 粘贴文本入库（切块 + 向量化） |
| `POST` | `/api/knowledge/url` | 抓取网址正文入库（无头浏览器 + 正文提取 + 插图转文字） |
| `GET` | `/api/knowledge/documents` | 已入库文档列表（按来源聚合，含块数与入库时间） |
| `DELETE` | `/api/knowledge/documents` | 按来源删除整篇文档的所有块 |
| `GET` | `/api/knowledge/images/{source_key}/{ingest_id}/{name}` | 取知识库原图（后端代理 Storage，返回 blob） |
| `GET` | `/api/health` | 健康检查 |

## 测试

```bash
cd backend
uv run pytest                          # 全部测试
uv run pytest tests/test_agent.py -k critic   # 按关键字筛选
```

测试不依赖真实 LLM 与网络：用假 langchain 模型注入 `service.chat_model`，数据层连真实 Postgres 测试库 `agent_test`（`SUPABASE_TEST_DATABASE_URL`，`tests/conftest.py` 负责建表与逐用例清表）；未配该变量时相关用例整体 skip。

## 如何扩展新业务

1. **业务规则（skill）**：在 `backend/app/skills/<业务名>/SKILL.md` 写业务规则，frontmatter 声明 `name` + `description`。清单会自动注入 system prompt，正文由 `get_skill` 工具按需加载——此步无需改动任何装配代码。

2. **工具（三步，缺一不可）**：
   1. **写数据源**：走 MCP 继承 `datasource/mcp_base.py` 的 `McpDataSource`（子类传 `url` + 认证头，实现查询方法）；走 HTTP 继承 `datasource/http_base.py` 的 `HttpDataSource`（基类封装了 `httpx.AsyncClient`）。
   2. **写注册函数**：在 `app/tools/` 下写 `register_xxx_tools(registry, ...)`，内部用 langchain 的 `@tool` 装饰工具函数（类型注解即参数 schema），再用 `registry.register_tool(tool_obj, "<中文动作短语>")` 登记，并返回内联 `new` 的数据源（供上层统一 `close`）。
      返回 dict/list 的工具用 `@tool(response_format="content_and_artifact")` 返回 `(content, raw)`：`content` 是模型可见文本（用 `render_content` 渲染），`raw` 保留原始结构供截断/spill/前端侧信道；返回 str 的工具保持普通 `@tool`。
   3. **登记**：在 `app/tools/__init__.py` 的 `register_all_tools()` 里加一行 `datasources.extend(xxx.register_xxx_tools(registry, ...))`。

   service 的 `_build_registry()` 只在启动时装配一次（数据源 `__init__` 会创建从不 `close` 的 httpx 客户端），新工具接入 `register_all_tools` 后即自动生效，无需改 service 装配代码。