# 数据库结构

后端持久化 = SQLite（`db_path`，默认 `app.db`）+ 文件存储（`result_dir`，地址索引）。

## 表结构（4 张业务表）

### conversations（会话元信息）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PRIMARY KEY | 会话 id（uuid） |
| `user_id` | TEXT | 所属用户 |
| `title` | TEXT | 会话标题 |
| `summary` | TEXT | 累积摘要（历史会话压缩，一个会话一条） |
| `path` | TEXT | 完整历史文件路径（历史会话压缩） |
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

### short_term_memory（短期记忆，会话内已查工具摘要）

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | 自增 id |
| `conversation_id` | TEXT | 所属会话 id |
| `tool_name` | TEXT | 工具名 |
| `args` | TEXT | 工具入参（JSON 字符串，规范化） |
| `summary` | TEXT | 一句话结果摘要 |
| `created_at` | TEXT | 创建时间 |

- 唯一索引 `idx_short_term_dedup(conversation_id, tool_name, args)`：同工具同参数覆盖旧行。

## 非表存储（文件）

### 地址索引（`result_dir` 文件）

| 项 | 说明 |
|---|---|
| 路径 | `result_dir/<user_id>/<conversation_id>/<rid>.txt` |
| 存 | 超长工具结果（`truncate_limit` 超阈值时）+ 历史答案（历史会话压缩） |
| 读 | 模型用 `read_file` / `grep` 按需读回 |
| 清理 | 本轮结束（`done` 前）与 `delete_conversation` 删该会话目录 |
