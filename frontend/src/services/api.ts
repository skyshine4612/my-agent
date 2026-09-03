// services/api.ts —— axios 封装 + 用户隔离 header
import axios from 'axios'

// 获取（或首次生成）用户唯一标识，用于后端按用户隔离数据
function getClientId(): string {
    let id = localStorage.getItem('client_id')
    // 不存在则用 crypto.randomUUID 生成并持久化到 localStorage
    if (!id) {
        id = crypto.randomUUID();
        localStorage.setItem('client_id', id)
    }
    return id
}

const api = axios.create({baseURL: '/api', timeout: 120000})
// 统一注入用户隔离 header：每个请求都带上 X-User-Id
api.interceptors.request.use(config => {
    config.headers['X-User-Id'] = getClientId();
    return config
})

// 会话列表
export async function listConversations() {
    return (await api.get('/conversations')).data
}

// 新建会话
export async function createConversation(title: string) {
    return (await api.post('/conversations', {title})).data
}

// 获取单个会话详情
export async function getConversation(id: string) {
    return (await api.get(`/conversations/${id}`)).data
}

// 删除会话
export async function deleteConversation(id: string) {
    return (await api.delete(`/conversations/${id}`)).data
}

// 长期记忆（跨会话偏好）
export async function getLongTermMemory() {
    return (await api.get('/memory/long-term')).data
}

// 短期记忆（某会话的 summary 摘要 + 最近几轮对话原文）
export async function getShortTermMemory(id: string) {
    return (await api.get(`/memory/short-term/${id}`)).data
}
