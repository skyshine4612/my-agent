// services/supabase.ts —— Supabase 客户端单例 + 取 access_token 的统一出口
// 登录由前端直连 GoTrue（不在 FastAPI 后面再包一层），换回的 JWT 再拿去调后端。
import {createClient} from '@supabase/supabase-js'

const url: string = import.meta.env.VITE_SUPABASE_URL ?? ''
const anonKey: string = import.meta.env.VITE_SUPABASE_ANON_KEY ?? ''

if (!url || !anonKey) {
    console.warn('[supabase] 缺少 VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY，登录不可用（参考 frontend/.env.example）')
}

// url 为空时 createClient 会抛错：给个占位地址让应用能起来，配置好后即正常
export const supabase = createClient(url || 'http://localhost:8000', anonKey || 'anon-key-missing', {
    auth: {
        // 会话持久化到 localStorage，到期自动刷新；业务侧只通过 getSession 取 token
        persistSession: true,
        autoRefreshToken: true,
    },
})

// 当前 access_token（临近过期的会由 supabase-js 自动刷新）；未登录返回 null
export async function getAccessToken(): Promise<string | null> {
    const {data} = await supabase.auth.getSession()
    return data.session?.access_token ?? null
}
