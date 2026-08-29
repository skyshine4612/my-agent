// types/index.ts —— 全局共享的 TypeScript 类型定义

// 工具事件：SSE 流中 tool_call / tool_result 的渲染单元
// tool_call 产生 {tool, args}，tool_result 随后把摘要回填到对应事件的 summary 字段
export interface ToolEvent {
  tool: string
  args?: Record<string, any>
  summary?: string
}

// 对话消息：前端聊天区的渲染单元
// user 消息只含文本；assistant 消息 = markdown 文本 + 一串工具气泡
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  tools?: ToolEvent[]
}
