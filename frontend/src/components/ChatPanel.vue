<script setup lang="ts">
// ChatPanel.vue —— 对话面板：渲染消息流 + 底部输入框；澄清提问渲染为可点击的追问气泡
import { ref } from 'vue'
import { Promotion } from '@element-plus/icons-vue'
import type { ChatMessage, TripPlanSet } from '@/types'

// 父级传入的消息列表与流式状态，向上 emit 发送/打开结果两个动作
// streaming：是否在流式请求中（禁用输入）；thinking：是否在等待助手首字（显示呼吸点）
const props = defineProps<{ messages: ChatMessage[]; streaming: boolean; thinking: boolean }>()
const emit = defineEmits<{
  send: [text: string]
  'open-result': [planSet: TripPlanSet]
}>()

// 输入框草稿
const draft = ref('')
// 输入框实例引用：点击澄清问题后聚焦输入框，引导用户补充答案
const inputRef = ref<any>(null)
// 输入框占位提示：点击澄清问题后切换为「补充答案」引导
const placeholder = ref('描述你的安排，回车发送…')

// 提交输入框内容：非空则发出 send，并清空草稿、复位占位提示
function submit() {
  const t = draft.value.trim()
  if (!t) return
  emit('send', t)
  draft.value = ''
  placeholder.value = '描述你的安排，回车发送…'
}

// 澄清追问：点击问题 → 预填进输入框并聚焦，让用户补充自己的答案后再作为 user 消息发送
// （绝不把提问原文直接回传后端）
function answer(q: string) {
  draft.value = q
  placeholder.value = '请在上方补充你的答案…'
  inputRef.value?.focus()
}

// 结果气泡上的「查看方案」：把该结果打开到右侧抽屉
function openResult(m: ChatMessage) {
  if (m.planSet) emit('open-result', m.planSet)
}

// 结果摘要文案：城市 + 方案数
function planSummary(planSet: TripPlanSet): string {
  const n = planSet.plans?.length ?? 0
  const city = planSet.plans[0]?.plan.city ?? ''
  return `已生成「${city}」${n} 套方案`
}
</script>

<template>
  <div class="chat-panel">
    <!-- 可滚动消息区 -->
    <div class="chat-panel__scroll">
      <div class="chat-panel__inner">
        <!-- 空态：尚未开始对话 -->
        <div v-if="!props.messages.length" class="chat-panel__empty">
          <p class="chat-panel__empty-title">今天想安排点什么？</p>
          <p class="chat-panel__empty-sub">
            例如：3000 元成都 4 天，或「带爸妈去杭州玩 3 天，预算 2000」
          </p>
        </div>

        <template v-for="m in props.messages" :key="m.id">
          <!-- 用户消息：右侧暖色气泡 -->
          <div v-if="m.role === 'user'" class="msg msg--user">
            <div class="msg__bubble msg__bubble--user">{{ m.content }}</div>
          </div>

          <!-- 助手澄清提问：追问气泡，问题可点击回答 -->
          <div v-else-if="m.kind === 'clarify'" class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <div class="msg__clarify">
              <p class="msg__clarify-text">想帮你安排得更妥帖，还需要确认几点：</p>
              <div class="msg__questions">
                <button
                  v-for="(q, i) in m.questions ?? []"
                  :key="i"
                  class="msg__question"
                  @click="answer(q)"
                >
                  {{ q }}
                </button>
              </div>
            </div>
          </div>

          <!-- 助手结果摘要：简洁一行 + 查看按钮，不抢占对话流 -->
          <div v-else-if="m.kind === 'result' && m.planSet" class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <div class="msg__result">
              <span class="msg__result-text">{{ planSummary(m.planSet) }}</span>
              <el-button size="small" round text type="primary" @click="openResult(m)">
                查看方案
              </el-button>
            </div>
          </div>

          <!-- 助手纯文本回复 -->
          <div v-else class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <div class="msg__text">{{ m.content }}</div>
          </div>
        </template>

        <!-- 编排进度面板（父级通过插槽注入，跟随消息流滚动） -->
        <slot name="progress" />

        <!-- 流式等待中的轻量提示 -->
        <div v-if="props.thinking" class="msg msg--assistant">
          <div class="msg__label">助手</div>
          <div class="msg__typing"><span></span><span></span><span></span></div>
        </div>
      </div>
    </div>

    <!-- 底部输入框 -->
    <div class="chat-panel__input">
      <el-input
        ref="inputRef"
        v-model="draft"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 4 }"
        :placeholder="placeholder"
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
.msg__text {
  max-width: 82%;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text);
}
.msg__clarify {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  background: var(--surface);
  max-width: 82%;
}
.msg__clarify-text {
  margin: 0 0 12px;
  color: var(--text-secondary);
  font-size: 14px;
}
.msg__questions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.msg__question {
  text-align: left;
  padding: 10px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  color: var(--text);
  cursor: pointer;
  font-size: 14px;
  transition: border-color 0.15s, background 0.15s;
}
.msg__question:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.msg__result {
  display: flex;
  align-items: center;
  gap: 12px;
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 12px 16px;
  background: var(--surface);
}
.msg__result-text {
  font-size: 15px;
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
