// router/index.ts —— 前端路由：直接进入对话主界面（记忆以弹窗形式内置，无需独立页面）
import {createRouter, createWebHistory} from 'vue-router'
import Chat from '@/views/Chat.vue'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', name: 'chat', component: Chat },
  ],
})

export default router
