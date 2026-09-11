// services/api.ts —— axios 封装 + 登录凭证注入
import axios from 'axios'
import {getAccessToken} from '@/services/supabase'

const api = axios.create({baseURL: '/api', timeout: 120000})
// 统一注入登录凭证：后端按 JWT 的 sub 判定用户身份
api.interceptors.request.use(async config => {
    const token = await getAccessToken()
    if (token) config.headers.Authorization = `Bearer ${token}`
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
