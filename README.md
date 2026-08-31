# 个人智能助手

多业务对话式 Agent 平台。后端基于 FastAPI 实现「通用 ReAct agent → 工具调用 → critic 事实校验」链路，前端是 Vue 3 通用对话界面。当前内置旅行助手业务，可自然扩展到更多业务场景。

## 功能特性

- **ReAct 推理-行动循环**：LLM 自主决策调用工具、并行执行、回填结果、再决策。
- **事实校验回路（critic）**：调用硬数据工具（车票/机票/天气）后，自动比对回答与工具结果，发现编造事实时基于真实结果重写答案。
- **真实数据源**：通过 ModelScope 托管的 MCP 服务接入高德地图、12306 火车票、variflight 机票、Bing 搜索。
- **标准 skill 两段式**：业务规则（`SKILL.md`）的清单常驻 system prompt，正文按需加载，新增业务无需改装配代码。
- **会话 + 长期记忆**：SQLite 持久化多轮对话；跨会话提炼用户稳定偏好，按 importance 召回并注入提示词。
- **SSE 流式对话**：逐字输出答案，实时展示工具调用进度。
- **用户隔离**：通过 `X-User-Id` 请求头按用户隔离会话与记忆数据。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+ · FastAPI · pydantic-settings · openai SDK · mcp · sse-starlette · sqlite3 |
| 前端 | Vue 3 · Vite · TypeScript · Element Plus · Pinia · marked + DOMPurify |
| LLM | 阿里云百炼 DashScope（`qwen-plus`，OpenAI 兼容协议） |
| 部署 | Docker + docker-compose · nginx |

## 目录结构

```
my-agent/
├── backend/
│   ├── app/
│   │   ├── api/routes/        # 路由：SSE 对话 + 会话管理
│   │   ├── core/              # 核心：agent(ReAct)/llm/critic/registry/memory/prompts/skills
│   │   ├── datasource/        # 数据源：高德/12306/机票/Bing 的 MCP 封装
│   │   ├── models/            # Pydantic 请求模型
│   │   ├── prompts/           # 提示词 .md（system/grounding/critic）
│   │   ├── services/          # AgentService 编排器
│   │   ├── skills/            # 业务 skill（travel/SKILL.md）
│   │   ├── tools/             # 工具注册（travel/network/system）
│   │   ├── config.py          # 配置（读取 .env）
│   │   └── main.py            # FastAPI 应用入口
│   ├── tests/                 # pytest 测试（不依赖真实 LLM/网络）
│   ├── pyproject.toml         # 依赖 + pytest 配置
│   └── run.py                 # 开发启动入口
├── frontend/
│   └── src/
│       ├── views/Chat.vue     # 主界面（SSE 事件分发 + 会话管理）
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

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_API_KEY` | LLM 服务 API Key | 空 |
| `LLM_BASE_URL` | LLM 接口地址 | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | 使用的模型 | `qwen-plus` |
| `MODELSCOPE_TOKEN` | ModelScope 访问令牌（调用 MCP 服务） | 空 |
| `AMAP_MCP_URL` | 高德地图 MCP 服务地址 | 空 |
| `TRAIN_12306_URL` | 12306 火车票 MCP 服务地址 | 空 |
| `FLIGHT_VARIFLIGHT_URL` | variflight 机票 MCP 服务地址 | 空 |
| `BING_MCP_URL` | Bing 网页搜索 MCP 服务地址 | 空 |
| `DB_PATH` | SQLite 数据库文件路径 | `app.db` |

## API 概览

所有接口以 `/api` 为前缀，通过请求头 `X-User-Id` 识别用户（缺省 `anonymous`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/agent/chat` | SSE 流式对话（事件：`conversation_id`/`token`/`tool_call`/`tool_result`/`status`/`done`） |
| `GET` | `/api/conversations` | 列出当前用户会话 |
| `POST` | `/api/conversations` | 新建会话 |
| `GET` | `/api/conversations/{id}` | 获取会话历史消息 |
| `DELETE` | `/api/conversations/{id}` | 删除会话 |
| `GET` | `/api/health` | 健康检查 |

## 测试

```bash
cd backend
uv run pytest                          # 全部测试
uv run pytest tests/test_llm.py -k stream   # 按关键字筛选
```

测试不依赖真实 LLM 与网络：用 `Fake*LLM` 注入 service，并用 `monkeypatch` 隔离 SQLite 到临时目录。

## 如何扩展新业务

1. 在 `backend/app/skills/<业务名>/SKILL.md` 写业务规则，frontmatter 声明 `name` + `description`。
2. 如需新工具，在 `backend/app/tools/` 下用 `registry.register(...)` 注册（数据源继承 `datasource/mcp_base.py` 的 `McpDataSource`）。

skill 清单会自动注入 system prompt，正文由 `get_skill` 工具按需加载，无需改动 service 装配代码。