# 数据库结构

后端持久化 = SQLite（`db_path`，默认 `app.db`）+ 文件存储（`result_dir`，地址索引）。

## 表结构（4 张业务表）

### conversations（会话元信息）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | 会话 id（uuid） |
| `user_id` | TEXT | 所属用户 |
| `title` | TEXT | 会话标题 |
| `created_at` | TEXT | 创建时间 |

### messages（消息记录）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 id |
| `conv_id` | TEXT | 所属会话 id |
| `role` | TEXT | `user` / `assistant` |
| `content` | TEXT | user 存纯文本；assistant 存 JSON 字符串（结构见下） |
| `created_at` | TEXT | 创建时间 |

`content` 字段（assistant 消息）的 JSON 结构：

```json
{
  "content": "答案 markdown 文本",   // 答案正文（前端还原气泡）
  "tools": [ToolItem, ...]           // 本轮工具列表，元素结构见下
}
```

`tools` 数组里每个元素（ToolItem）的结构：

```json
{
  "tool": "recipe_detail",          // 工具名
  "args": {"id": "3356352"},        // 工具入参
  "label": "搜菜谱做法",             // 中文动作短语
  "status": true,                   // 是否执行完成
  "result": "截断版结果"             // 工具结果截断版（前端气泡展开）
}
```

### long_term_memory（长期记忆，跨会话偏好）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 id |
| `user_id` | TEXT | 所属用户 |
| `fact` | TEXT | 偏好/事实 |
| `importance` | REAL | 重要度 0~1 |
| `created_at` | TEXT | 创建时间 |

### short_term_memory（短期记忆，摘要 + 最近几轮对话）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 id（决定展示顺序） |
| `conversation_id` | TEXT | 所属会话 id |
| `role` | TEXT | `summary`（摘要）/ `user` / `assistant`（对话原文） |
| `content` | TEXT | 摘要文本 或 对话原文 |
| `created_at` | TEXT | 写入时间 |

- 一个会话多行：1 条 `summary`（早期对话压缩摘要，排最前）+ 最近几轮对话原文（每条一条）。
- `replace_records` 每轮清空重写，保证「摘要 vs 原文」边界清晰、跨轮持久。

## 非表存储（文件）

### 地址索引（`result_dir` 文件）

| 项 | 说明 |
|---|---|
| 路径 | `result_dir/<user_id>/<conversation_id>/<rid>.txt` |
| 存 | 超长工具结果（`truncate_limit` 超阈值时）+ 历史答案（历史会话压缩） |
| 读 | 模型用 `read_file` / `grep` 按需读回 |
| 清理 | spill 文件本轮结束清（保留 `_history.txt`）；`delete_conversation` 删整个目录（含 `_history.txt`） |
