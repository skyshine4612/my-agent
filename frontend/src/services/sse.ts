// services/sse.ts —— SSE 流式消费
function getClientId(): string {
  let id = localStorage.getItem('client_id')
  if (!id) { id = crypto.randomUUID(); localStorage.setItem('client_id', id) }
  return id
}

// 向后端发起流式对话，逐条解析 SSE 事件并通过 onEvent 回调返回
export async function chatStream(message: string, conversationId: string | null, onEvent: (ev: any) => void) {
  const resp = await fetch('/api/agent/chat', {
    method: 'POST',
    // 用户隔离 header 与请求体
    headers: { 'Content-Type': 'application/json', 'X-User-Id': getClientId() },
    body: JSON.stringify({ message, conversation_id: conversationId })
  })
  const reader = resp.body!.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    // 流式解码，避免多字节字符被切断
    buf += decoder.decode(value, { stream: true })
    // 规范化换行：兼容 \r\n / \r / \n，统一成 \n 再按空行分帧
    const normalized = buf.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    const parts = normalized.split('\n\n')
    // 最后一个可能是不完整的事件，保留到下一轮
    buf = parts.pop()!
    for (const p of parts) {
      const ev = parseEvent(p)
      if (ev) onEvent(ev)
    }
  }
  // 流结束：处理 buf 里残留的最后一个事件（可能没有以空行结尾）
  const last = parseEvent(buf)
  if (last) onEvent(last)
}

// 从一段 SSE 帧里提取 data: 行并解析 JSON
function parseEvent(frame: string): any | null {
  const dataLine = frame.split('\n').find((l) => l.startsWith('data:'))
  if (!dataLine) return null
  try {
    return JSON.parse(dataLine.slice(5).trim())
  } catch {
    return null
  }
}
