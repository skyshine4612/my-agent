// types/index.ts —— 全局共享的 TypeScript 类型定义

// 会话文件：上传件与脚本产物同形（后端 FileStore.save 的返回）
export interface StoredFile {
    file_id: string
    name: string
    size: number
}

// 知识库图片：key 是 Storage 对象 key（上传失败时为 null），desc 是多模态模型的描述，
// ref 是正文里的引用号（chunk 行 id - 块内下标，如 "13-2"）—— 上传失败、或正文里没有
// 对应标记的图没有 ref，也就永远不会被正文引用到
export interface KnowledgeImage {
    key: string | null
    desc: string
    ref?: string
}

// 工具事件：SSE 流中 tool_call / tool_result 的渲染单元
// tool_call 产生 {tool, args, id}，tool_result 随后按 id 回填 status（是否完成）与 result（截断版结果）
// id 用于同名工具并发时精确配对（历史落库消息不含 id，故设为可选）
export interface ToolEvent {
    tool: string
    label?: string
    args?: Record<string, any>
    status?: boolean
    result?: string
    id?: string
    // 产出这个调用的是哪个 Agent：主 Agent 为 'assistant'，委派出去的子 Agent 为 'sub'。
    // 子 Agent 的工具调用与主 Agent 走同一条 SSE，靠这个字段区分层级
    agent?: string
    // 归属键：子 Agent 的事件带上发起它的那次 call_sub_agent 的 id，前端据此把子任务的
    // 工具气泡与阶段状态挂到对应的委派节点下（并行委派时靠它区分是哪个子任务）
    parent?: string
    // 委派节点的当前阶段（整理中/核对事实/修正答案）：随子 Agent 的 status 事件更新，
    // 是流式瞬态，刷新后从历史还原时不恢复
    phase?: string
    // 后端落库时把 id 存成 tool_call_id（供组装 role=tool 消息配对），还原历史时由前端归一成 id
    tool_call_id?: string
    // 脚本产物：沙箱里生成、后端写回会话文件目录的文件，渲染成下载卡片
    artifacts?: StoredFile[]
    // 知识库图片：与 artifacts 并列的侧信道，渲染成缩略图（不可下载）
    images?: KnowledgeImage[]
}

// 对话消息：前端聊天区的渲染单元
// user 消息 = 文本 + 随消息发出的附件（附件随消息落库，刷新、切会话后仍在）
// assistant 消息 = markdown 文本 + 一串工具气泡
export interface ChatMessage {
    id: string
    role: 'user' | 'assistant'
    content: string
    files?: StoredFile[]
    tools?: ToolEvent[]
}

// 长期记忆条目：跨会话偏好（fact + 重要度）
export interface LongTermMemory {
    fact: string
    importance: number
}

// 短期记忆记录：一条 summary 摘要 或 一条对话原文
export interface ShortTermMemoryRecord {
    role: 'summary' | 'user' | 'assistant'
    content: string
}
