// services/sse.ts —— SSE 流式消费
import {getAccessToken} from '@/services/supabase'

// 401：登录态已失效，调用方据此清会话并引导重新登录
export class AuthExpiredError extends Error {
    constructor() {
        super('登录已失效，请重新登录')
    }
}

// 向后端发起流式对话，逐条解析 SSE 事件并通过 onEvent 回调返回
export async function chatStream(message: string, conversationId: string | null, onEvent: (ev: any) => void) {
    // 登录凭证：后端按 JWT 的 sub 判定用户身份
    const token = await getAccessToken()
    const headers: Record<string, string> = {'Content-Type': 'application/json'}
    if (token) headers.Authorization = `Bearer ${token}`

    const resp = await fetch('/api/agent/chat', {
        method: 'POST',
        headers,
        body: JSON.stringify({message, conversation_id: conversationId})
    })
    // 出错的响应体不是 SSE，先拦下来，避免下游把错误 JSON 当事件流解析
    if (resp.status === 401) throw new AuthExpiredError()
    if (!resp.ok) throw new Error(`请求失败（HTTP ${resp.status}）`)

    const reader = resp.body!.getReader()
    const decoder = new TextDecoder()
    let buf = ''
    while (true) {
        const {done, value} = await reader.read()
        if (done) break
        // 流式解码，避免多字节字符被切断
        buf += decoder.decode(value, {stream: true})
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
