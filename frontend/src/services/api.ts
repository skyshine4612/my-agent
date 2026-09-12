// services/api.ts —— axios 封装 + 登录凭证注入
import axios from 'axios'
import {getAccessToken} from '@/services/supabase'

const api = axios.create({baseURL: '/api', timeout: 120000})
// 建库要爬页面再逐批向量化，耗时远超普通接口（几十秒到几分钟），单独放宽
const INGEST_TIMEOUT = 600000
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

// 知识库文档列表（按来源聚合，含块数）
export async function listDocuments() {
    return (await api.get('/knowledge/documents')).data
}

// 粘贴文本入库（文件也走这条：前端读成文本后当正文提交）
export async function ingestText(content: string, title: string) {
    return (await api.post('/knowledge/text', {content, title}, {timeout: INGEST_TIMEOUT})).data
}

// 抓取网址正文入库（后端用无头浏览器打开页面，插图会转成文字描述）
export async function ingestUrl(url: string) {
    return (await api.post('/knowledge/url', {url}, {timeout: INGEST_TIMEOUT})).data
}

// 按来源删除整篇文档的所有片段
export async function deleteDocument(sourceUrl: string) {
    return (await api.delete('/knowledge/documents', {params: {source_url: sourceUrl}})).data
}
