// router/index.ts —— 前端路由：登录页 + 对话主界面（记忆以弹窗形式内置，无需独立页面）
import {createRouter, createWebHistory} from 'vue-router'
import Chat from '@/views/Chat.vue'
import Login from '@/views/Login.vue'
import {useAuthStore} from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'chat', component: Chat, meta: { requiresAuth: true } },
    { path: '/login', name: 'login', component: Login },
  ],
})

// 未登录只能停在登录页，已登录不再回登录页
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (auth.loading) await auth.init()
  if (to.meta.requiresAuth && !auth.session) return {name: 'login'}
  if (to.name === 'login' && auth.session) return {name: 'chat'}
})

export default router
