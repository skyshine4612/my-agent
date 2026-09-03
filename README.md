# 个人智能助手

多业务对话式 Agent 平台。后端基于 FastAPI 实现「通用 ReAct agent → 工具调用 → critic 事实校验」链路，前端是 Vue 3 通用对话界面。当前内置旅行助手、饮食规划、网页搜索、节假日、天气、翻译、热榜，可通过接入更多工具自然扩展业务场景。

## 功能特性

- **ReAct 推理-行动循环**：LLM 自主决策调用工具、并行执行、回填结果、再决策。
- **事实校验回路（critic）**：调用硬数据工具（车票/机票/天气/节假日）后，多轮带工具自动比对回答与完整工具结果，发现编造事实时基于真实结果重写答案。
- **真实数据源**：高德（POI/推荐菜，Web 服务 API）、天气/节假日/翻译/热榜/菜谱/营养（UAPIS，天气最多 7 天预报）、搜索（Tavily）经 HTTP 直连；12306 火车票经 ModelScope 托管 MCP；机票直连 variflight 官方 MCP。
- **标准 skill 两段式**：业务规则（`SKILL.md`）的清单常驻 system prompt，正文按需加载，新增业务无需改装配代码。
- **子 Agent 委派**：复杂子任务可委派给独立上下文的子 Agent 处理、只返回摘要，避免主上下文被大量工具结果淹没。
- **三层记忆**：工作记忆（本轮工具结果，易失）+ 短期记忆（summary 摘要 + 最近几轮对话，独立表持久化）+ 长期记忆（跨会话偏好），短期/长期记忆与会话历史由 SQLite 持久化；短期记忆超预算压成摘要，工作记忆超长 spill（摘要 + 文件地址索引），子 Agent 按子会话隔离工具结果地址索引。
- **长结果地址索引 + 历史会话压缩**：工具结果超阈值时完整结果写临时文件，模型用 `read_file`/`grep` 按需读回；历史会话按 running summary 压缩——一个会话的短期记忆 = 累积摘要 + 最近几轮原文 + 完整历史文件，上下文到阈值才触发，早期答案压成摘要、最近几轮保留原文，跨轮引用靠 `read_file` 读回，既避免截断丢信息，也避免模型凭标题编造细节。
- **记忆查看**：输入框旁「记忆」入口，弹窗内 tab 切换长期偏好与当前会话的短期记忆（摘要 + 最近几轮对话）。
- **SSE 流式对话**：逐字输出答案，实时展示工具调用进度；完整回复在流式输出前已落库，刷新后自动恢复会话与历史。
- **用户隔离**：通过 `X-User-Id` 请求头按用户隔离会话与记忆数据。

## 技术栈

| 层 | 技术                                                                                    |
|---|-----------------------------------------------------------------------------------------|
| 后端 | Python 3.11+ · FastAPI · pydantic-settings · openai SDK · mcp · sse-starlette · sqlite3 |
| 前端 | Vue 3 · Vite · TypeScript · Element Plus · Pinia · marked + DOMPurify                   |
| LLM | 阿里云百炼 DashScope（`qwen3.7-plus`，OpenAI 兼容协议）                                 |
| 部署 | Docker + docker-compose · nginx                                                         |

## 目录结构

```
my-agent/
├── backend/
│   ├── app/
│   │   ├── api/routes/        # 路由：SSE 对话 + 会话管理
│   │   ├── core/              # 核心：agent(ReAct)/llm/critic/registry/memory(三层记忆)/prompts/skills
│   │   ├── datasource/        # 数据源：MCP（12306/机票）+ HTTP（高德/UAPIS/Tavily）
│   │   ├── models/            # Pydantic 请求模型
│   │   ├── prompts/           # 提示词 .md（system/grounding/critic）
│   │   ├── services/          # AgentService 编排器
│   │   ├── skills/            # 业务 skill（travel/meal_planning 等）
│   │   ├── tools/             # 工具注册（travel/network/common/system/sub_agent）
│   │   ├── config.py          # 配置（读取 .env）
│   │   └── main.py            # FastAPI 应用入口
│   ├── tests/                 # pytest 测试（不依赖真实 LLM/网络）
│   ├── pyproject.toml         # 依赖 + pytest 配置
│   └── run.py                 # 开发启动入口
├── frontend/
│   └── src/
│       ├── views/Chat.vue     # 主界面（SSE 事件分发 + 会话管理 + 记忆弹窗）
│       ├── components/        # ChatPanel（markdown + 工具气泡）
│       ├── services/          # api.ts / sse.ts
│       ├── stores/            # Pinia
│       └── types/             # TS 类型定义
├── docker-compose.yml
└── README.md
```

## 快速开始

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
uv run python run.py    # 启动于 http://0.0.0.0:8000（reload）
```

> 未配置 `LLM_API_KEY` 时后端仍可启动，但对话返回空内容；填入 key 后重启即可。

**3. 启动前端**（需 Node 22+）

```bash
cd frontend
npm install
npm run dev             # 启动于 http://127.0.0.1:5173，/api 代理到 8000
```

浏览器访问前端地址即可对话。

### Docker 部署

```bash
docker compose up --build
```

- 前端 nginx 对外唯一入口 `:80`，反代 `/api`（含 SSE）到后端 `:8000`。
- 后端读取挂载的 `./backend/.env`（密钥不写进 compose），SQLite 持久化到 `./data`。

## 配置说明

`backend/.env` 支持的配置项：

| 变量 | 说明 | 默认值                                              |
|---|---|-----------------------------------------------------|
| `LLM_API_KEY` | LLM 服务 API Key | 空                                                  |
| `LLM_BASE_URL` | LLM 接口地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | 使用的模型 | `qwen3.7-plus`                                      |
| `MODELSCOPE_TOKEN` | ModelScope 访问令牌（调用 MCP 服务） | 空                                                  |
| `TRAIN_12306_URL` | 12306 火车票 MCP 服务地址 | 空                                                  |
| `FLIGHT_VARIFLIGHT_URL` | variflight 官方 MCP 服务地址 | 空                                                  |
| `VARIFLIGHT_API_KEY` | variflight 官方 API Key（X-API-Key 认证） | 空                                                  |
| `UAPIS_API_KEY` | UAPIS 令牌（节假日/翻译/热榜/菜谱/营养；天气接口免费无需 key） | 空                                                  |
| `TAVILY_API_KEY` | Tavily API Key（搜索/网页提取） | 空                                                  |
| `AMAP_API_KEY` | 高德 Web 服务 Key（POI/推荐菜） | 空                                                  |
| `DB_PATH` | SQLite 数据库文件路径 | `app.db`                                            |
| `LLM_CONTEXT_BUDGET` | 工作记忆 token 预算上限（超限淘汰最老 + LLM 摘要兜底） | `32000`                                             |
| `TRUNCATE_LIMIT` | 工具结果字符串截断阈值（超限完整结果写文件，read_file/grep 按需读） | `4000`                                              |
| `RESULT_DIR` | 工具结果地址索引的落盘目录（本轮用完即删） | `results`                                           |

### 密钥获取来源

| 变量 | 获取网站 / 路径 |
|---|---|
| `LLM_API_KEY` | [阿里云百炼控制台](https://bailian.console.aliyun.com) → API-KEY 管理 |
| `MODELSCOPE_TOKEN` | [ModelScope 魔搭社区](https://modelscope.cn) → 个人中心 → 访问令牌（Access Token） |
| `VARIFLIGHT_API_KEY` | [飞常准飞友 AI 开放平台](https://ai.variflight.com) → 注册后「API Keys」页（[ai.variflight.com/keys](https://ai.variflight.com/keys)） |
| `UAPIS_API_KEY` | [UAPIS 开放接口平台](https://uapis.cn) → 注册后控制台（[uapis.cn/console](https://uapis.cn/console)）「API keys」标签页 |
| `TAVILY_API_KEY` | [Tavily](https://app.tavily.com) → 注册登录后「API Keys」页 |
| `AMAP_API_KEY` | [高德开放平台](https://lbs.amap.com) → 控制台 → 应用管理 → 创建应用添加 Key（Web 服务） |

## API 概览

所有接口以 `/api` 为前缀，通过请求头 `X-User-Id` 识别用户（缺省 `anonymous`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/chat` | SSE 流式对话（事件：`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`） |
| `GET` | `/api/conversations` | 列出当前用户会话 |
| `POST` | `/api/conversations` | 新建会话 |
| `GET` | `/api/conversations/{id}` | 获取会话历史消息 |
| `DELETE` | `/api/conversations/{id}` | 删除会话 |
| `GET` | `/api/memory/long-term` | 当前用户长期记忆（跨会话偏好） |
| `GET` | `/api/memory/short-term/{conversation_id}` | 某会话短期记忆（summary 摘要 + 最近几轮对话） |
| `GET` | `/api/health` | 健康检查 |

## 测试

```bash
cd backend
uv run pytest                          # 全部测试
uv run pytest tests/test_llm.py -k stream   # 按关键字筛选
```

测试不依赖真实 LLM 与网络：用 `Fake*LLM` 注入 service，并用 `monkeypatch` 隔离 SQLite 到临时目录。

## 如何扩展新业务

1. **业务规则（skill）**：在 `backend/app/skills/<业务名>/SKILL.md` 写业务规则，frontmatter 声明 `name` + `description`。清单会自动注入 system prompt，正文由 `get_skill` 工具按需加载——此步无需改动任何装配代码。

2. **工具（三步，缺一不可）**：
   1. **写数据源**：走 MCP 继承 `datasource/mcp_base.py` 的 `McpDataSource`（子类传 `url` + 认证头，实现查询方法）；走 HTTP 继承 `datasource/http_base.py` 的 `HttpDataSource`（基类封装了 `httpx.AsyncClient`）。
   2. **写注册函数**：在 `app/tools/` 下写 `register_xxx_tools(registry, ...)`，内部用 `registry.register(name, description, parameters, fn, label)` 逐个注册，并返回内联 `new` 的数据源（供上层统一 `close`）。
   3. **登记**：在 `app/tools/__init__.py` 的 `register_all_tools()` 里加一行 `datasources.extend(xxx.register_xxx_tools(registry, ...))`。

   service 的 `_build_registry()` 只在启动时装配一次（数据源 `__init__` 会创建从不 `close` 的 httpx 客户端），新工具接入 `register_all_tools` 后即自动生效，无需改 service 装配代码。