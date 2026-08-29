<script setup lang="ts">
// ChatPanel.vue —— 通用对话面板：渲染 markdown 消息流 + 工具气泡 + 底部输入框
import { ref } from 'vue'
import { Promotion } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { ChatMessage } from '@/types'

// 父级传入的消息列表与流式状态，向上 emit 发送动作
// streaming：是否在流式请求中（禁用输入）；thinking：是否在等待助手首字（显示呼吸点）
const props = defineProps<{ messages: ChatMessage[]; streaming: boolean; thinking: boolean }>()
const emit = defineEmits<{ send: [text: string] }>()

// 输入框草稿
const draft = ref('')

// 提交输入框内容：非空则发出 send，并清空草稿
function submit() {
  const t = draft.value.trim()
  if (!t) return
  emit('send', t)
  draft.value = ''
}

// 把 markdown 文本渲染为安全的 HTML：先 marked 转 HTML，再 DOMPurify 清洗防 XSS
function renderMarkdown(text: string): string {
  const raw = marked.parse(text, { async: false }) as string
  return DOMPurify.sanitize(raw)
}

// 工具参数在摘要行里的紧凑展示：超长截断，完整参数放进气泡展开区
function formatArgs(args: Record<string, any>): string {
  const s = JSON.stringify(args)
  return s.length > 60 ? `${s.slice(0, 60)}…` : s
}
</script>

<template>
  <div class="chat-panel">
    <!-- 可滚动消息区 -->
    <div class="chat-panel__scroll">
      <div class="chat-panel__inner">
        <!-- 空态：尚未开始对话 -->
        <div v-if="!props.messages.length" class="chat-panel__empty">
          <p class="chat-panel__empty-title">今天想聊点什么？</p>
          <p class="chat-panel__empty-sub">输入你的问题，回车发送，助手会流式回复并展示工具调用过程</p>
        </div>

        <template v-for="m in props.messages" :key="m.id">
          <!-- 用户消息：右侧暖色气泡 -->
          <div v-if="m.role === 'user'" class="msg msg--user">
            <div class="msg__bubble msg__bubble--user">{{ m.content }}</div>
          </div>

          <!-- 助手消息：markdown 正文 + 工具气泡 -->
          <div v-else class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <!-- markdown 正文（token 流式累积） -->
            <div v-if="m.content" class="msg__markdown" v-html="renderMarkdown(m.content)"></div>
            <!-- 工具气泡：tool_call 显示「调用 工具名(参数)」，tool_result 回填摘要；可折叠、持久化 -->
            <details v-for="(t, i) in m.tools ?? []" :key="i" class="tool">
              <summary class="tool__head">
                <span class="tool__status" :class="t.summary ? 'tool__status--done' : 'tool__status--run'"></span>
                <span class="tool__name">调用 {{ t.tool }}</span>
                <span v-if="t.args && Object.keys(t.args).length" class="tool__args">{{ formatArgs(t.args) }}</span>
              </summary>
              <div class="tool__body">
                <pre v-if="t.args && Object.keys(t.args).length" class="tool__params">{{ JSON.stringify(t.args, null, 2) }}</pre>
                <div v-if="t.summary" class="tool__summary">{{ t.summary }}</div>
                <div v-else class="tool__running">执行中…</div>
              </div>
            </details>
          </div>
        </template>

        <!-- 流式等待中的轻量提示（呼吸点） -->
        <div v-if="props.thinking" class="msg msg--assistant">
          <div class="msg__label">助手</div>
          <div class="msg__typing"><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>

    <!-- 底部输入框 -->
    <div class="chat-panel__input">
      <el-input
        v-model="draft"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        placeholder="描述你的问题，回车发送…"
        :disabled="props.streaming"
        @keydown.enter.exact.prevent="submit"
      />
      <el-button
        class="chat-panel__send"
        type="primary"
        :icon="Promotion"
        :loading="props.streaming"
        circle
        @click="submit"
      />
    </div>
  </div>
</template>

<style scoped>
.chat-panel {
  height: 100%;
  display: flex;
  flex-direction: column;
}
.chat-panel__scroll {
  flex: 1;
  overflow-y: scroll;   /* 始终显示滚动条，避免滚动条出现消失导致抖动 */
}
.chat-panel__inner {
  max-width: 760px;
  margin: 0 auto;
  padding: 32px 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.chat-panel__empty {
  padding: 48px 0;
}
.chat-panel__empty-title {
  font-size: 22px;
  font-weight: 600;
  margin: 0 0 8px;
}
.chat-panel__empty-sub {
  color: var(--text-secondary);
  margin: 0;
  line-height: 1.7;
}
.msg {
  display: flex;
  flex-direction: column;
}
.msg--user {
  align-items: flex-end;
}
.msg--assistant {
  align-items: flex-start;
}
.msg__bubble {
  max-width: 72%;
  padding: 12px 16px;
  border-radius: 16px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.msg__bubble--user {
  background: var(--accent-soft);
  color: var(--text);
  border-bottom-right-radius: 4px;
}
.msg__label {
  font-size: 12px;
  color: var(--accent);
  font-weight: 600;
  margin-bottom: 6px;
}
/* markdown 正文：注入的 HTML 用 :deep() 才能命中 scoped 之外的子元素 */
.msg__markdown {
  max-width: 82%;
  line-height: 1.8;
  color: var(--text);
  word-break: break-word;
}
.msg__markdown :deep(p) {
  margin: 0 0 12px;
}
.msg__markdown :deep(p:last-child) {
  margin-bottom: 0;
}
.msg__markdown :deep(h1),
.msg__markdown :deep(h2),
.msg__markdown :deep(h3),
.msg__markdown :deep(h4) {
  margin: 16px 0 8px;
  line-height: 1.4;
}
.msg__markdown :deep(h1) {
  font-size: 20px;
}
.msg__markdown :deep(h2) {
  font-size: 18px;
}
.msg__markdown :deep(h3) {
  font-size: 16px;
}
.msg__markdown :deep(h4) {
  font-size: 15px;
}
.msg__markdown :deep(pre) {
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
  margin: 12px 0;
}
.msg__markdown :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
  font-size: 13px;
  background: var(--bg);
  padding: 2px 6px;
  border-radius: 4px;
}
.msg__markdown :deep(pre code) {
  background: transparent;
  padding: 0;
}
.msg__markdown :deep(ul),
.msg__markdown :deep(ol) {
  padding-left: 20px;
  margin: 8px 0;
}
.msg__markdown :deep(li) {
  margin: 4px 0;
}
.msg__markdown :deep(blockquote) {
  border-left: 3px solid var(--border);
  margin: 12px 0;
  padding-left: 12px;
  color: var(--text-secondary);
}
.msg__markdown :deep(a) {
  color: var(--accent);
}
.msg__markdown :deep(table) {
  border-collapse: collapse;
  margin: 12px 0;
}
.msg__markdown :deep(th),
.msg__markdown :deep(td) {
  border: 1px solid var(--border);
  padding: 6px 10px;
}
/* 工具气泡：用原生 details/summary 实现折叠，展开状态由 DOM 自身持久化 */
.tool {
  max-width: 82%;
  margin-top: 8px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  overflow: hidden;
}
.tool__head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  cursor: pointer;
  list-style: none;   /* 去掉 summary 默认三角标记 */
  user-select: none;
}
.tool__head::-webkit-details-marker {
  display: none;
}
.tool__status {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tool__status--run {
  background: var(--accent);
  animation: blink 1.2s infinite ease-in-out;
}
.tool__status--done {
  background: #16a34a;
}
.tool__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
}
.tool__args {
  font-size: 12px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}
.tool__body {
  padding: 8px 12px 12px;
  border-top: 1px dashed var(--border);
}
.tool__params {
  margin: 0 0 8px;
  font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  color: var(--text-secondary);
  background: var(--bg);
  border-radius: 6px;
  padding: 8px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
.tool__summary {
  font-size: 13px;
  color: var(--text);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.tool__running {
  font-size: 13px;
  color: var(--text-secondary);
}
.msg__typing {
  display: flex;
  gap: 5px;
}
.msg__typing span {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-secondary);
  opacity: 0.5;
  animation: blink 1.2s infinite ease-in-out;
}
.msg__typing span:nth-child(2) {
  animation-delay: 0.2s;
}
.msg__typing span:nth-child(3) {
  animation-delay: 0.4s;
}
@keyframes blink {
  0%,
  80%,
  100% {
    opacity: 0.25;
  }
  40% {
    opacity: 0.9;
  }
}
.chat-panel__input {
  border-top: 1px solid var(--border);
  padding: 16px 24px 20px;
  display: flex;
  gap: 12px;
  align-items: flex-end;
  background: var(--bg);
}
.chat-panel__send {
  flex-shrink: 0;
}
</style>
