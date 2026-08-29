<script setup lang="ts">
// Chat.vue —— 对话主界面：左侧窄会话列表 + 中间对话区 + 右侧结果抽屉
// 负责 SSE 事件分发（conversation_id/clarification/plan_start/task_*/agent_*/tool_*/final_result）、
// 会话切换、结果抽屉与导出（图片/PDF）
import { computed, onMounted, ref } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { useConversationStore } from '@/stores/conversation'
import { useTripStore } from '@/stores/trip'
import { chatStream } from '@/services/sse'
import { listConversations, getConversation, deleteConversation } from '@/services/api'
import ChatPanel from '@/components/ChatPanel.vue'
import ProgressPanel from '@/components/ProgressPanel.vue'
import TripResultPanel from '@/components/TripResultPanel.vue'
import type { ChatMessage, TripPlanSet } from '@/types'

const convStore = useConversationStore()
const tripStore = useTripStore()

// 聊天消息列表
const messages = ref<ChatMessage[]>([])
// 是否正在流式请求中
const streaming = ref(false)
// 结果抽屉开关
const drawerOpen = ref(false)

// 消息 id 自增计数器，保证渲染 key 唯一
let seq = 0
function nextId(): string {
  seq += 1
  return `m${Date.now()}-${seq}`
}

// 是否有编排进度（收到 plan_start）→ 控制呼吸点与进度面板的切换
const hasProgress = computed(() => tripStore.events.some((e) => e.type === 'plan_start'))
// 呼吸点：流式请求中、且尚未收到 plan_start（助手在拆解任务）
const thinking = computed(() => streaming.value && !hasProgress.value)

// —— 消息构造辅助 ——
function pushUser(text: string) {
  messages.value.push({ id: nextId(), role: 'user', kind: 'text', content: text })
}
function pushAssistantText(text: string) {
  messages.value.push({ id: nextId(), role: 'assistant', kind: 'text', content: text })
}
function pushClarify(questions: string[]) {
  messages.value.push({ id: nextId(), role: 'assistant', kind: 'clarify', questions })
}
function pushResult(planSet: TripPlanSet) {
  messages.value.push({ id: nextId(), role: 'assistant', kind: 'result', planSet })
}

// 刷新会话列表（后端未启动时静默失败）
async function refreshConversations() {
  try {
    convStore.conversations = await listConversations()
  } catch {
    convStore.conversations = []
  }
}

// 发送消息：调用 chatStream，SSE 事件实时驱动进度面板与结果面板
async function send(text: string) {
  const t = text.trim()
  if (!t || streaming.value) return
  pushUser(t)
  streaming.value = true
  // 清空上一轮进度与结果，收起结果抽屉，开始新一轮规划
  tripStore.events = []
  tripStore.planSet = null
  drawerOpen.value = false
  try {
    await chatStream(t, convStore.currentId, (ev: any) => {
      // 记录全部事件供 ProgressPanel 读取（含思考过程）
      tripStore.events.push(ev)
      // 事件分发：按 type 更新会话 / 澄清提问 / 最终结果
      switch (ev.type) {
        case 'conversation_id':
          // 首次对话拿到会话 id，记住并刷新侧栏
          convStore.currentId = ev.conversation_id
          refreshConversations()
          break
        case 'clarification':
          // 信息不全：渲染提问气泡，等用户补充后继续同一会话
          pushClarify(ev.questions)
          break
        case 'final_result':
          // 整合完成：写入方案 store、渲染结果气泡并打开抽屉
          tripStore.planSet = ev.data
          pushResult(ev.data)
          openResult()
          break
      }
    })
  } catch {
    pushAssistantText('抱歉，规划暂时失败，请稍后重试。')
  } finally {
    streaming.value = false
  }
}

// 新对话：重置会话与结果状态
function newChat() {
  convStore.currentId = null
  localStorage.removeItem('current_conv_id')   // 清除持久化的会话
  tripStore.planSet = null
  tripStore.events = []
  messages.value = []
  drawerOpen.value = false
}

// 删除会话：调后端删除，删除的是当前会话则重置
async function deleteConv(id: string) {
  try {
    await deleteConversation(id)
    if (convStore.currentId === id) {
      newChat()
    }
    await refreshConversations()
  } catch {
    /* 静默失败 */
  }
}

// 切换会话：加载历史消息并回填聊天区
async function selectConversation(id: string) {
  convStore.currentId = id
  localStorage.setItem('current_conv_id', id)   // 持久化当前会话，刷新后恢复
  tripStore.planSet = null
  tripStore.events = []
  drawerOpen.value = false
  messages.value = []
  try {
    const history = await getConversation(id)
    messages.value = historyToMessages(history)
  } catch {
    messages.value = []
  }
}

// 后端历史消息 → 前端 ChatMessage（assistant 的 JSON 结果解析为 result 气泡 / 澄清气泡）
function historyToMessages(history: { role: string; content: string }[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const m of history) {
    if (m.role === 'user') {
      out.push({ id: nextId(), role: 'user', kind: 'text', content: m.content })
    } else {
      const planSet = tryParsePlanSet(m.content)
      if (planSet) {
        out.push({ id: nextId(), role: 'assistant', kind: 'result', planSet })
      } else {
        const questions = tryParseClarify(m.content)
        if (questions) out.push({ id: nextId(), role: 'assistant', kind: 'clarify', questions })
        else out.push({ id: nextId(), role: 'assistant', kind: 'text', content: m.content })
      }
    }
  }
  return out
}

// 尝试把 assistant 消息解析为多套方案 JSON（失败则返回 null，按纯文本处理）
function tryParsePlanSet(content: string): TripPlanSet | null {
  try {
    const o = JSON.parse(content)
    if (o && typeof o === 'object' && Array.isArray(o.plans)) return o as TripPlanSet
  } catch {
    /* 非 JSON 文本，忽略 */
  }
  return null
}

// 尝试把 assistant 消息解析为澄清问题 JSON（失败则返回 null，按纯文本处理）
function tryParseClarify(content: string): string[] | null {
  try {
    const o = JSON.parse(content)
    if (o && o.clarify === true && Array.isArray(o.questions)) return o.questions
  } catch {
    /* 非 JSON 文本，忽略 */
  }
  return null
}

// 打开结果抽屉
function openResult() {
  drawerOpen.value = true
}

// 点击结果气泡「查看方案」：把该结果写回 store 并打开抽屉（支持历史会话）
function showResult(planSet: TripPlanSet) {
  tripStore.planSet = planSet
  openResult()
}

// 会话时间戳格式化：仅显示 月-日 时:分
function shortDate(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(async () => {
  await refreshConversations()
  // 刷新后恢复上次会话：从 localStorage 读取当前会话 id 并加载历史消息
  const saved = localStorage.getItem('current_conv_id')
  if (saved) {
    await selectConversation(saved)
  }
})
</script>

<template>
  <div class="chat">
    <!-- 左侧窄会话列表 -->
    <aside class="chat__side">
      <div class="brand">
        <span class="brand__dot"></span>
        <span class="brand__name">日常助手</span>
      </div>
      <el-button class="chat__new" text :icon="Plus" @click="newChat">新对话</el-button>

      <nav class="conv-list">
        <div
          v-for="c in convStore.conversations"
          :key="c.id"
          class="conv"
          :class="{ 'conv--active': c.id === convStore.currentId }"
          @click="selectConversation(c.id)"
        >
          <span class="conv__title">{{ c.title || '新会话' }}</span>
          <span class="conv__date">{{ shortDate(c.created_at) }}</span>
          <button class="conv__del" title="删除会话" @click.stop="deleteConv(c.id)">×</button>
        </div>
        <p v-if="!convStore.conversations.length" class="conv__empty">暂无会话</p>
      </nav>
    </aside>

    <!-- 中间对话区：核心，占主导 -->
    <section class="chat__main">
      <ChatPanel
        :messages="messages"
        :streaming="streaming"
        :thinking="thinking"
        @send="send"
        @open-result="showResult"
      >
        <!-- 编排进度注入消息流 -->
        <template #progress>
          <ProgressPanel v-if="streaming && hasProgress" />
        </template>
      </ChatPanel>
    </section>

    <!-- 右侧结果抽屉 -->
    <el-drawer
      v-model="drawerOpen"
      direction="rtl"
      size="480px"
      :with-header="false"
      class="result-drawer"
    >
      <div v-if="tripStore.planSet" class="result">
        <header class="result__head">
          <div>
            <div class="result__title">行程方案</div>
            <div class="result__city">{{ tripStore.planSet.plans[0]?.plan.city }}</div>
          </div>
        </header>

        <div class="result__body">
          <TripResultPanel :plan-set="tripStore.planSet" />
        </div>
      </div>
      <p v-else class="result__none">暂无行程方案</p>
    </el-drawer>
  </div>
</template>

<style scoped>
.chat {
  height: 100%;
  display: flex;
}
/* 左侧会话列表：窄侧栏，暖白底 + 右边框，不抢主区 */
.chat__side {
  width: 220px;
  flex-shrink: 0;
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 20px 12px;
  gap: 8px;
  background: var(--surface);
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 8px 12px;
}
.brand__dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent);
}
.brand__name {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.5px;
}
.chat__new {
  justify-content: flex-start;
  margin-bottom: 8px;
}
.conv-list {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.conv {
  padding: 10px 12px;
  border-radius: 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 2px;
  transition: background 0.15s;
  position: relative;
}
.conv:hover {
  background: var(--bg);
}
.conv--active {
  background: var(--accent-soft);
}
.conv__title {
  font-size: 14px;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conv__date {
  font-size: 12px;
  color: var(--text-secondary);
}
.conv__del {
  position: absolute;
  right: 8px;
  top: 50%;
  transform: translateY(-50%);
  border: none;
  background: transparent;
  color: var(--text-secondary);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
}
.conv:hover .conv__del {
  opacity: 1;
}
.conv__del:hover {
  color: #dc2626;
}
.conv__empty {
  color: var(--text-secondary);
  font-size: 13px;
  padding: 8px;
}
/* 中间对话区：占满剩余空间 */
.chat__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
/* 结果抽屉内部排版 */
.result {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.result__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 4px 0 12px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 12px;
}
.result__title {
  font-size: 16px;
  font-weight: 700;
}
.result__city {
  font-size: 13px;
  color: var(--text-secondary);
  margin-top: 4px;
}
.result__actions {
  display: flex;
  gap: 4px;
}
.result__body {
  flex: 1;
  overflow: hidden;
}
.result__none {
  color: var(--text-secondary);
  text-align: center;
  padding: 40px 0;
}
</style>
