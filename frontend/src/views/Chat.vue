<script setup lang="ts">
// Chat.vue —— 通用对话主界面：左侧窄会话列表 + 中间对话区
// 负责 SSE 事件分发（conversation_id/token/tool_call/tool_result/done）与会话管理
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Plus } from '@element-plus/icons-vue'
import { useConversationStore } from '@/stores/conversation'
import { useAuthStore } from '@/stores/auth'
import { chatStream, AuthExpiredError } from '@/services/sse'
import { listConversations, getConversation, deleteConversation, getLongTermMemory, getShortTermMemory, uploadFile, fetchFile } from '@/services/api'
import ChatPanel from '@/components/ChatPanel.vue'
import KnowledgeDialog from '@/components/KnowledgeDialog.vue'
import type { ChatMessage, ToolEvent, LongTermMemory, ShortTermMemoryRecord, StoredFile } from '@/types'

const convStore = useConversationStore()
const auth = useAuthStore()
const router = useRouter()

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
  attached.value = []   // 消息发出，附件条收起（文件本身留在会话里）
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
              t.artifacts = ev.artifacts   // 脚本产物：渲染成下载卡片
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
  } catch (e) {
    // 登录态失效：清掉本地会话，由路由守卫把用户送回登录页
    if (e instanceof AuthExpiredError) {
      await auth.signOut()
      await router.replace({ name: 'login' })
      return
    }
    pushAssistantText('抱歉，本次请求失败，请稍后重试。')
  } finally {
    streaming.value = false
    phase.value = ''
  }
}

// 退出登录：清会话后回登录页
async function logout() {
  await auth.signOut()
  await router.replace({ name: 'login' })
}

// 新对话：重置会话状态
function newChat() {
  convStore.currentId = null
  localStorage.removeItem('current_conv_id')   // 清除持久化的会话
  messages.value = []
  attached.value = []   // 换会话了，上一会话的文件不该再提示
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
  attached.value = []   // 换会话了，上一会话的文件不该再提示
  try {
    const history = await getConversation(id)
    messages.value = historyToMessages(history)
  } catch {
    messages.value = []
  }
}

// 已上传但还没发消息的文件：输入区上方提示，发出消息后收起（文件本身留在会话里）
const attached = ref<StoredFile[]>([])
const uploading = ref(false)

// 上传文件：后端在「没带会话 id」或「带的不是自己的会话」时会新建一个，返回值里给出真正的 id，
// 前端必须接管它 —— 否则上传件落在 A 会话、接下来的对话在 B 会话，模型看不到这个文件
async function handleUpload(file: File) {
  if (uploading.value) return
  uploading.value = true
  try {
    const saved = await uploadFile(file, convStore.currentId)
    if (saved.conversation_id !== convStore.currentId) {
      convStore.currentId = saved.conversation_id
      localStorage.setItem('current_conv_id', saved.conversation_id)
      await refreshConversations()
    }
    attached.value = [saved]
  } catch {
    pushAssistantText('文件上传失败：本期只收 .xlsx，单文件不超过 20MB。')
  } finally {
    uploading.value = false
  }
}

// 下载产物：先取 blob 再本地另存为 —— <a href="/api/files/..."> 带不上 Bearer，会 401
async function downloadArtifact(f: StoredFile) {
  if (!convStore.currentId) return
  try {
    const blob = await fetchFile(f.file_id, convStore.currentId)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = f.name
    a.click()
    URL.revokeObjectURL(url)
  } catch {
    pushAssistantText('文件下载失败，可能这个文件已经被删除了。')
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

// 知识库弹窗：添加资料（粘贴/文件/网址）与已入库列表都在组件内部，这里只管开关
const knowledgeVisible = ref(false)

// —— 记忆弹窗（长期/短期 tab 切换）——
// 入口在 ChatPanel 的发送按钮左侧；短期记忆跟随当前会话，长期记忆为全局偏好
const memoryVisible = ref(false)
const activeMemoryTab = ref('long')
const longTerm = ref<LongTermMemory[]>([])
const shortTerm = ref<ShortTermMemoryRecord[]>([])

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
  try { const res = await getShortTermMemory(convStore.currentId); shortTerm.value = res.records || [] } catch { shortTerm.value = [] }
}

// 短期记忆角色 → 中文标签映射
const MEMORY_ROLE_LABEL: Record<string, string> = {
  summary: '摘要',
  user: '用户',
  assistant: '助手',
}

// 短期记忆记录的展示形态：assistant 带 tool_calls 的记录显示为「工具调用」+ 工具名(入参) 列表
function memoryView(r: ShortTermMemoryRecord): { label: string; content: string } {
  try {
    const data = JSON.parse(r.content)
    if (data && typeof data === 'object' && Array.isArray(data.tool_calls)) {
      const calls = data.tool_calls
        .map((tc: any) => {
          const fn = tc?.function
          if (!fn?.name) return ''
          let args = ''
          try {
            args = fn.arguments ? JSON.stringify(JSON.parse(fn.arguments)) : ''
          } catch { args = fn.arguments || '' }
          return args ? `${fn.name}(${args})` : fn.name
        })
        .filter((s: string) => s)
      return { label: '工具调用', content: calls.join('、') }
    }
  } catch { /* 非 JSON，按答案展示 */ }
  return { label: MEMORY_ROLE_LABEL[r.role] ?? r.role, content: r.content }
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

      <div class="side-user">
        <span class="side-user__mail" :title="auth.displayName">{{ auth.displayName }}</span>
        <button class="side-user__out" @click="logout">退出</button>
      </div>
    </aside>

    <!-- 中间对话区：核心，占主导 -->
    <section class="chat__main">
      <ChatPanel :messages="messages" :streaming="streaming" :thinking="thinking" :phase="phase"
                 :attached="attached" :uploading="uploading"
                 @send="send" @upload="handleUpload" @download="downloadArtifact"
                 @open-memory="openMemory" @open-knowledge="knowledgeVisible = true" />
    </section>

    <!-- 记忆弹窗：长期（全局偏好）/短期（当前会话 running summary）tab 切换 -->
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
          <div v-else-if="shortTerm.length" class="memory-records">
            <div v-for="(r, i) in shortTerm" :key="i" class="memory-record" :class="`memory-record--${r.role}`">
              <span class="memory-record__role">{{ memoryView(r).label }}</span>
              <span class="memory-record__content">{{ memoryView(r).content }}</span>
            </div>
          </div>
          <p v-else class="memory-empty">当前会话还没有短期记忆（running summary）</p>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>

    <!-- 知识库弹窗：与「记忆」并列，管理检索用的攻略语料 -->
    <KnowledgeDialog v-model="knowledgeVisible" />
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
/* 侧栏底部：当前账号 + 退出登录 */
.side-user {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 8px 0;
  border-top: 1px solid var(--border);
}
.side-user__mail {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.side-user__out {
  flex-shrink: 0;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.side-user__out:hover {
  color: var(--accent-strong);
  border-color: var(--accent);
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
/* 短期记忆记录：卡片列表，summary 用浅琥珀底强调，与长期记忆卡片风格一致 */
.memory-records {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 50vh;
  overflow-y: auto;
}
.memory-record {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 14px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--border);
}
.memory-record__role {
  font-size: 12px;
  color: var(--text-secondary);
}
.memory-record__content {
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.memory-record--summary {
  background: var(--accent-soft);
  border-color: var(--accent);
}
/* 中间对话区：占满剩余空间 */
.chat__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}
</style>
