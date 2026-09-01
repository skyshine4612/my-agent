<script setup lang="ts">
// Chat.vue —— 通用对话主界面：左侧窄会话列表 + 中间对话区
// 负责 SSE 事件分发（conversation_id/token/tool_call/tool_result/done）与会话管理
import { computed, onMounted, ref } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { useConversationStore } from '@/stores/conversation'
import { chatStream } from '@/services/sse'
import { listConversations, getConversation, deleteConversation, getLongTermMemory, getShortTermMemory } from '@/services/api'
import ChatPanel from '@/components/ChatPanel.vue'
import type { ChatMessage, ToolEvent, LongTermMemory, ShortTermMemory } from '@/types'

const convStore = useConversationStore()

// 聊天消息列表
const messages = ref<ChatMessage[]>([])
// 是否正在流式请求中
const streaming = ref(false)
// 本轮是否已产生助手输出（token / 工具事件）→ 控制呼吸点显示
const assistantOutput = ref(false)
// 工具调用结束后的阶段文案（整理答案 / 核对事实 / 修正答案），空串表示不在这些阶段
const phase = ref('')
// status 事件 → 前端展示的阶段文案
const PHASE_LABELS: Record<string, string> = {
  generating: '正在整理答案…',
  checking: '正在核对事实…',
  correcting: '正在修正答案…',
}

// 消息 id 自增计数器，保证渲染 key 唯一
let seq = 0
function nextId(): string {
  seq += 1
  return `m${Date.now()}-${seq}`
}

// 呼吸点：流式请求中、且尚未进入任何阶段（phase）、也未吐出 token/工具事件
// 一旦进入生成/核对/修正阶段，改用 phase 文案展示，避免「助手」呼吸点与阶段文案同时出现
const thinking = computed(() => streaming.value && !assistantOutput.value && !phase.value)

// —— 消息构造辅助 ——
function pushUser(text: string) {
  messages.value.push({ id: nextId(), role: 'user', content: text })
}
function pushAssistantText(text: string) {
  messages.value.push({ id: nextId(), role: 'assistant', content: text })
}

// 刷新会话列表（后端未启动时静默失败）
async function refreshConversations() {
  try {
    convStore.conversations = await listConversations()
  } catch {
    convStore.conversations = []
  }
}

// 发送消息：调用 chatStream，把 token/tool_call/tool_result 累积到当前助手消息
async function send(text: string) {
  const t = text.trim()
  if (!t || streaming.value) return
  pushUser(t)
  streaming.value = true
  assistantOutput.value = false
  phase.value = ''

  // 当前正在构建的助手消息：本轮所有 token 与工具事件都累积到它身上
  let current: ChatMessage | null = null
  // 懒创建：首条 token 或首个工具事件到达时才生成助手消息，避免空轮次留下空白气泡
  const ensureAssistant = (): ChatMessage => {
    if (!current) {
      current = { id: nextId(), role: 'assistant', content: '', tools: [] }
      messages.value.push(current)
    }
    return current
  }

  try {
    await chatStream(t, convStore.currentId, (ev: any) => {
      switch (ev.type) {
        case 'conversation_id':
          // 首次对话拿到会话 id：记住 + 持久化到 localStorage（刷新后能恢复这个会话）+ 刷新侧栏
          convStore.currentId = ev.conversation_id
          localStorage.setItem('current_conv_id', ev.conversation_id)
          refreshConversations()
          break
        case 'status':
          // 工具调用结束后的各阶段（整理/核对/修正）：映射成文案展示，避免 critic 等阶段长静默
          phase.value = PHASE_LABELS[ev.status] ?? ''
          break
        case 'token':
          // 逐字增量：追加到助手消息 content；答案开始输出，结束「整理中」状态
          ensureAssistant().content += ev.content ?? ''
          assistantOutput.value = true
          phase.value = ''
          break
        case 'tool_call':
          // 工具调用：追加一个气泡（带 id，供 tool_result 精确配对），稍后回填摘要；工具进度替代「整理中」呼吸点
          ensureAssistant().tools!.push({ tool: ev.tool, label: ev.label, args: ev.args, id: ev.id })
          assistantOutput.value = true
          phase.value = ''
          break
        case 'tool_result': {
          // 回填结果：优先按 id 精确匹配（同名工具并发也不串），id 缺失时回退按工具名。
          // （不调用 ensureAssistant，避免无匹配时新建 content/tools 全空的气泡）
          if (!current) break
          const list = current.tools!
          for (let i = list.length - 1; i >= 0; i--) {
            const t = list[i]!
            if (t.status) continue
            const matched = ev.id ? t.id === ev.id : t.tool === ev.tool
            if (matched) {
              t.status = ev.status
              t.result = ev.result
              assistantOutput.value = true
              break
            }
          }
          break
        }
        case 'done':
          // 本轮结束：无需额外处理，finally 里会复位 streaming
          break
      }
    })
  } catch {
    pushAssistantText('抱歉，本次请求失败，请稍后重试。')
  } finally {
    streaming.value = false
    phase.value = ''
  }
}

// 新对话：重置会话状态
function newChat() {
  convStore.currentId = null
  localStorage.removeItem('current_conv_id')   // 清除持久化的会话
  messages.value = []
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
  messages.value = []
  try {
    const history = await getConversation(id)
    messages.value = historyToMessages(history)
  } catch {
    messages.value = []
  }
}

// 后端历史消息 → 前端 ChatMessage（assistant 的 {content, tools} JSON 还原为 markdown + 工具气泡）
function historyToMessages(history: { role: string; content: string }[]): ChatMessage[] {
  const out: ChatMessage[] = []
  for (const m of history) {
    if (m.role === 'user') {
      out.push({ id: nextId(), role: 'user', content: m.content })
    } else {
      const parsed = parseAssistantContent(m.content)
      if (parsed) {
        out.push({ id: nextId(), role: 'assistant', content: parsed.content, tools: parsed.tools })
      } else {
        out.push({ id: nextId(), role: 'assistant', content: m.content })
      }
    }
  }
  return out
}

// 尝试把 assistant 消息解析为 {content, tools} JSON（失败则返回 null，按纯文本处理）
function parseAssistantContent(content: string): { content: string; tools?: ToolEvent[] } | null {
  try {
    const o = JSON.parse(content)
    if (o && typeof o === 'object' && typeof o.content === 'string') {
      // 过滤历史脏数据：tools 里可能混入 null / 非对象 / tool 非字符串的条目，
      // 保留形状合法的元素，避免渲染「调用 undefined」气泡
      return { content: o.content, tools: Array.isArray(o.tools) ? o.tools.filter((t: any) => t && typeof t.tool === 'string') : undefined }
    }
  } catch {
    /* 非 JSON 文本，忽略 */
  }
  return null
}

// 会话时间戳格式化：仅显示 月-日 时:分
function shortDate(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

// —— 记忆弹窗（长期/短期 tab 切换）——
// 入口在 ChatPanel 的发送按钮左侧；短期记忆跟随当前会话，长期记忆为全局偏好
const memoryVisible = ref(false)
const activeMemoryTab = ref('long')
const longTerm = ref<LongTermMemory[]>([])
const shortTerm = ref<ShortTermMemory[]>([])

// 打开记忆弹窗并加载长期 + 当前会话短期记忆
function openMemory() {
  memoryVisible.value = true
  loadLongTerm()
  loadShortTerm()
}
async function loadLongTerm() {
  try { longTerm.value = await getLongTermMemory() } catch { longTerm.value = [] }
}
async function loadShortTerm() {
  if (!convStore.currentId) { shortTerm.value = []; return }
  try { shortTerm.value = await getShortTermMemory(convStore.currentId) } catch { shortTerm.value = [] }
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
      <ChatPanel :messages="messages" :streaming="streaming" :thinking="thinking" :phase="phase" @send="send" @open-memory="openMemory" />
    </section>

    <!-- 记忆弹窗：长期（全局偏好）/短期（当前会话已查工具）tab 切换 -->
    <el-dialog v-model="memoryVisible" title="记忆" width="640px">
      <el-tabs v-model="activeMemoryTab">
        <el-tab-pane label="长期记忆" name="long">
          <p v-if="!longTerm.length" class="memory-empty">暂无长期记忆（跨会话偏好）</p>
          <div v-else class="memory-fact-list">
            <div v-for="(f, i) in longTerm" :key="i" class="memory-fact">
              <span class="memory-fact__text">{{ f.fact }}</span>
              <el-tag size="small" type="info">重要度 {{ (f.importance * 100).toFixed(0) }}%</el-tag>
            </div>
          </div>
        </el-tab-pane>
        <el-tab-pane label="短期记忆" name="short">
          <p v-if="!convStore.currentId" class="memory-empty">请先开始一个会话，短期记忆跟随当前会话</p>
          <el-table v-else-if="shortTerm.length" :data="shortTerm" size="small" border>
            <el-table-column prop="tool_name" label="工具" width="160" />
            <el-table-column prop="args" label="参数" min-width="180" />
            <el-table-column prop="summary" label="摘要" min-width="180" />
          </el-table>
          <p v-else class="memory-empty">当前会话还没有已查记录</p>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
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
.memory-empty {
  color: var(--text-secondary);
  font-size: 14px;
  padding: 16px 0;
}
.memory-fact-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.memory-fact {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
}
.memory-fact__text {
  font-size: 14px;
}
/* 中间对话区：占满剩余空间 */
.chat__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
</style>
