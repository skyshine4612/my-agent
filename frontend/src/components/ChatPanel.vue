<script setup lang="ts">
// ChatPanel.vue —— 通用对话面板：渲染 markdown 消息流 + 工具气泡 + 底部输入框
import { ref } from 'vue'
import { Promotion, Collection, ArrowDown } from '@element-plus/icons-vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { ChatMessage, ToolEvent } from '@/types'

// 父级传入的消息列表与流式状态，向上 emit 发送动作
// streaming：是否在流式请求中（禁用输入）；thinking：是否在等待助手首字（显示呼吸点）
// phase：工具调用完成后的阶段文案（整理答案/核对事实/修正答案），空串表示不在这些阶段
const props = defineProps<{ messages: ChatMessage[]; streaming: boolean; thinking: boolean; phase: string }>()
const emit = defineEmits<{ send: [text: string]; 'open-memory': [] }>()

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
  // breaks: true 让单换行也渲染成 <br>：否则 LLM 输出里的单换行（如逐日安排、逐餐厅）会被吞掉挤成一行
  const raw = marked.parse(text, { async: false, breaks: true }) as string
  return DOMPurify.sanitize(raw)
}

// 工具气泡文案：label 是后端注册工具时给出的中文动作短语（如「查询火车票」），
// 前端统一按执行状态拼「正在{label}… / 已{label}」，无 label 时回退英文名。
function toolLabel(t: ToolEvent, done: boolean): string {
  const name = t.label || t.tool
  return done ? `已${name}` : `正在${name}…`
}

// 展开的工具气泡 key 集合（`消息id-工具索引`），点击气泡切换展开/收起 result
const expanded = ref<Set<string>>(new Set())
function toggleTool(key: string) {
  const next = new Set(expanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expanded.value = next
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

          <!-- 助手消息：工具进度（上）+ markdown 正文（下），按时间顺序「先查询、后回答」 -->
          <div v-else class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <!-- 工具进度：显示在答案之前，一行简洁中文文案 + 状态点（执行中闪烁 / 完成变绿）；
                 完成态可点击展开查看完整（截断版）结果 result -->
            <div v-for="(t, i) in m.tools ?? []" :key="i" class="tool-wrap">
              <div class="tool" @click="toggleTool(`${m.id}-${i}`)">
                <span class="tool__status" :class="t.status ? 'tool__status--done' : 'tool__status--run'"></span>
                <span class="tool__name">{{ toolLabel(t, !!t.status) }}</span>
                <span v-if="t.status && t.result" class="tool__caret" :class="{ 'tool__caret--open': expanded.has(`${m.id}-${i}`) }">
                  <el-icon><ArrowDown /></el-icon>
                </span>
              </div>
              <div v-if="expanded.has(`${m.id}-${i}`) && t.result" class="tool__result">
                <pre>{{ t.result }}</pre>
              </div>
            </div>
            <!-- markdown 正文（token 流式累积） -->
            <div v-if="m.content" class="msg__markdown" v-html="renderMarkdown(m.content)"></div>
          </div>
        </template>

        <!-- 流式等待中的轻量提示（呼吸点） -->
        <div v-if="props.thinking" class="msg msg--assistant">
          <div class="msg__label">助手</div>
          <div class="msg__typing"><span></span><span></span><span></span></div>
        </div>
        <!-- 工具调用完成后的阶段（整理答案/核对事实/修正答案）：呼吸点 + 阶段文案，让用户知道在做什么 -->
        <div v-if="props.phase" class="msg msg--assistant">
          <div class="msg__phase">
            <span class="msg__typing"><span></span><span></span><span></span></span>
            <span class="msg__phase-text">{{ props.phase }}</span>
          </div>
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
        class="chat-panel__memory"
        :icon="Collection"
        circle
        title="记忆"
        @click="emit('open-memory')"
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
/* 工具进度：一行简洁文案 + 状态点（执行中闪烁 / 完成变绿），完成态可点击展开 result */
.tool-wrap {
  max-width: 82%;
  margin-top: 8px;
}
.tool {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  cursor: pointer;
}
.tool__caret {
  margin-left: auto;
  color: var(--text-secondary);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  transition: transform 0.2s;
}
.tool__caret--open {
  transform: rotate(180deg);
}
.tool__result {
  margin-top: 4px;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  overflow-x: auto;
}
.tool__result pre {
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
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
.msg__phase {
  display: flex;
  align-items: center;
  gap: 8px;
}
.msg__phase-text {
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
.chat-panel__memory {
  flex-shrink: 0;
}
</style>
