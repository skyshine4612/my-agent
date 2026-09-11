// stores/auth.ts —— 登录态：GoTrue 会话、当前用户、登录/注册/登出
import {computed, ref} from 'vue'
import {defineStore} from 'pinia'
import type {Session} from '@supabase/supabase-js'
import {supabase} from '@/services/supabase'

// GoTrue 只认邮箱/手机号登录，没有用户名字段；用户名统一映射到该域下的假邮箱。
// mailer_autoconfirm 已开，这个域收不到信也不影响注册。
const USERNAME_DOMAIN = '@my-agent.local'

function toEmail(username: string): string {
    return `${username.trim().toLowerCase()}${USERNAME_DOMAIN}`
}

// GoTrue 的错误文案是英文硬编码，改不了服务端，只能在前端按 error_code 翻。
// 匹配 code 而非文案：GoTrue 改文案不会让映射失效。未收录的 code 回退原文。
const AUTH_ERROR_MESSAGES: Record<string, string> = {
    invalid_credentials: '用户名或密码不正确',
    user_already_exists: '该用户名已被占用',
    email_exists: '该用户名已被占用',
    weak_password: '密码太弱，至少 6 位',
    validation_failed: '用户名格式不合法',
    signup_disabled: '注册功能已关闭',
    email_not_confirmed: '账号尚未激活',
    over_request_rate_limit: '尝试过于频繁，请稍后再试',
    session_not_found: '登录状态已失效，请重新登录',
    refresh_token_not_found: '登录状态已失效，请重新登录',
}

function toChineseMessage(error: {code?: string; message: string}): string {
    return (error.code && AUTH_ERROR_MESSAGES[error.code]) || error.message
}

export const useAuthStore = defineStore('auth', () => {
    const session = ref<Session | null>(null)
    // 首次 getSession 返回前为 true：路由守卫据此等待，避免登录态未定时被误判成未登录而闪回登录页
    const loading = ref(true)

    let started = false

    function apply(s: Session | null) {
        session.value = s
        loading.value = false
    }

    // 读取初始会话并订阅后续变化（token 刷新、其他标签页登出）
    async function init() {
        if (started) return
        started = true
        const {data} = await supabase.auth.getSession()
        apply(data.session)
        supabase.auth.onAuthStateChange((_event, s) => apply(s))
    }

    async function signIn(username: string, password: string) {
        const {error} = await supabase.auth.signInWithPassword({email: toEmail(username), password})
        if (error) throw new Error(toChineseMessage(error))
        const {data} = await supabase.auth.getSession()
        apply(data.session)
    }

    async function signUp(username: string, password: string) {
        const {data, error} = await supabase.auth.signUp({email: toEmail(username), password})
        if (error) throw new Error(toChineseMessage(error))
        // 开了邮箱自动确认时直接带回会话；否则要再去登录页登一次
        if (data.session) apply(data.session)
    }

    async function signOut() {
        await supabase.auth.signOut()
        apply(null)
    }

    // 界面展示用：剥掉假邮箱后缀还原成用户名
    const displayName = computed(() => {
        const mail = session.value?.user.email ?? ''
        return mail.endsWith(USERNAME_DOMAIN) ? mail.slice(0, -USERNAME_DOMAIN.length) : mail
    })

    return {session, loading, displayName, init, signIn, signUp, signOut}
})
