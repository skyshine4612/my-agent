// types/index.ts —— 全局共享的 TypeScript 类型定义

// 会话文件：上传件与脚本产物同形（后端 FileStore.save 的返回）
export interface StoredFile {
    file_id: string
    name: string
    size: number
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
    // 脚本产物：沙箱里生成、后端写回会话文件目录的文件，渲染成下载卡片
    artifacts?: StoredFile[]
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
