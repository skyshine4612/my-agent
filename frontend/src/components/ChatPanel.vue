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
          <!-- 用户消息：右侧暖色气泡（带附件时先在气泡里列出随这条消息发出的文件） -->
          <div v-if="m.role === 'user'" class="msg msg--user">
            <div class="msg__bubble msg__bubble--user">
              <div v-if="m.files?.length" class="msg__files">
                <span v-for="f in m.files" :key="f.file_id" class="msg__file" @click="emit('download', f)">
                  {{ f.name }}（{{ formatSize(f.size) }}）
                </span>
              </div>
              <span>{{ m.content }}</span>
            </div>
          </div>

          <!-- 助手消息：工具进度（上）+ markdown 正文（中）+ 产物（下），按「先查询、后回答、最后交付」排列 -->
          <div v-else class="msg msg--assistant">
            <div class="msg__label">助手</div>
            <!-- 工具进度：显示在答案之前，一行简洁中文文案 + 状态点（执行中闪烁 / 完成变绿）；
                 完成态可点击展开查看完整（截断版）结果 result -->
            <template v-for="(t, i) in m.tools ?? []" :key="keyOf(m, t, i)">
              <!-- 主 Agent 自己的工具气泡；带 parent 的是子 Agent 的条目，放到对应委派节点下渲染 -->
              <div v-if="!t.parent" class="tool-wrap">
                <div class="tool" :class="{ 'tool--static': !canToggle(t) }" @click="toggleNode(m, t, i)">
                  <span class="tool__status" :class="t.status ? 'tool__status--done' : 'tool__status--run'"></span>
                  <span class="tool__name">{{ toolLabel(t, !!t.status) }}</span>
                  <span v-if="t.phase" class="tool__phase">{{ t.phase }}</span>
                  <!-- 有东西可收才显示箭头（canToggle 为假时连手型都不给） -->
                  <span v-if="canToggle(t)" class="tool__caret"
                        :class="{ 'tool__caret--open': isOpen(m, t, i) }">
                    <el-icon><ArrowDown /></el-icon>
                  </span>
                </div>
                <!-- 委派气泡的折叠区：只收它自己的结果（子任务返回的那段回答）与配图。
                     紧跟在委派气泡下面——回答是这次委派的结论，点气泡收放的也只有它 -->
                <template v-if="isOpen(m, t, i)">
                  <div v-if="t.result" class="tool__result">
                    <pre>{{ t.result }}</pre>
                  </div>
                  <!-- 知识库图片：检索到的图，不可下载、只做缩略图。与完整结果共用气泡的展开态——
                       一次检索可能回十几张，常显会把对话刷成图墙；正文里引用的那几张由
                       MessageMarkdown 就地渲染，才是给用户看的 -->
                  <KnowledgeThumbs v-if="t.images?.length" :images="t.images" />
                </template>
                <!-- 委派出去的子 Agent：它自己的 ReAct 进度（工具调用 + 阶段状态）缩进挂在回答之下。
                     这一段**不参与委派气泡的折叠**：折叠收的只是上面那段回答，进度要一直看得见 -->
                <div v-for="(c, j) in childrenOf(m, t)" :key="keyOf(m, c, j)" class="tool-wrap tool-wrap--sub">
                  <div class="tool" :class="{ 'tool--static': !canToggle(c) }" @click="toggleTool(keyOf(m, c, j))">
                    <span class="tool__status" :class="c.status ? 'tool__status--done' : 'tool__status--run'"></span>
                    <span class="tool__name">{{ toolLabel(c, !!c.status) }}</span>
                    <span v-if="c.status && (c.result || c.images?.length)" class="tool__caret" :class="{ 'tool__caret--open': expanded.has(keyOf(m, c, j)) }">
                      <el-icon><ArrowDown /></el-icon>
                    </span>
                  </div>
                  <div v-if="expanded.has(keyOf(m, c, j)) && c.result" class="tool__result">
                    <pre>{{ c.result }}</pre>
                  </div>
                  <KnowledgeThumbs v-if="expanded.has(keyOf(m, c, j)) && c.images?.length" :images="c.images" />
                </div>
              </div>
            </template>
            <!-- markdown 正文（token 流式累积）：知识库图片按正文里的引用号就地渲染 -->
            <div v-if="m.content" class="msg__markdown">
              <MessageMarkdown :content="m.content" :images="imagesOf(m)" />
            </div>
            <!-- 生成的文件：独立交付物区块，挂在正文之后。文件一生成就渲染，不等本轮 done ——
                 工具执行完文件就在了，而正文之后还要过 critic 校验，没道理让用户干等 -->
            <div v-if="artifactsOf(m).length" class="artifacts">
              <div class="artifacts__title">生成的文件</div>
              <div v-for="a in artifactsOf(m)" :key="a.file_id" class="artifact" @click="emit('download', a)">
                <el-icon class="artifact__icon"><Document /></el-icon>
                <span class="artifact__name">{{ a.name }}</span>
                <span class="artifact__size">{{ formatSize(a.size) }}</span>
                <span class="artifact__action">下载</span>
              </div>
            </div>
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

    <!-- 底部：附件条 + 输入行共用一个底色与顶边框 -->
    <div class="chat-panel__bottom">
      <div v-if="props.attached.length" class="chat-panel__attached">
        <span v-for="f in props.attached" :key="f.file_id" class="attach">
          已上传 {{ f.name }}（{{ formatSize(f.size) }}），发消息告诉我怎么处理
        </span>
      </div>

      <div class="chat-panel__input">
        <el-input
          v-model="draft"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 4 }"
          placeholder="描述你的问题，回车发送…"
          :disabled="props.streaming"
          @keydown.enter.exact.prevent="submit"
        />
        <!-- 上传入口：accept 只过滤文件选择器，真正的后缀与大小校验在后端 -->
        <input ref="fileInput" type="file" accept=".xlsx" class="chat-panel__file" @change="pickFile" />
        <el-button
          class="chat-panel__memory"
          :icon="Paperclip"
          circle
          title="上传文件（.xlsx）"
          :disabled="props.streaming || props.uploading"
          @click="fileInput?.click()"
        />
        <el-button
          class="chat-panel__memory"
          :icon="Collection"
          circle
          title="记忆"
          @click="emit('open-memory')"
        />
        <el-button
          class="chat-panel__memory"
          :icon="Reading"
          circle
          title="知识库"
          @click="emit('open-knowledge')"
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
  </div>
</template>

<script setup lang="ts">
// ChatPanel.vue —— 通用对话面板：渲染 markdown 消息流 + 工具气泡 + 底部输入框
import { ref } from 'vue'
import { Promotion, Collection, Reading, ArrowDown, Paperclip, Document } from '@element-plus/icons-vue'
import type { ChatMessage, ToolEvent, StoredFile } from '@/types'
import KnowledgeThumbs from '@/components/KnowledgeThumbs.vue'
import MessageMarkdown from '@/components/MessageMarkdown.vue'

// 父级传入的消息列表与流式状态，向上 emit 发送动作
// streaming：是否在流式请求中（禁用输入）；thinking：是否在等待助手首字（显示呼吸点）
// phase：工具调用完成后的阶段文案（整理答案/核对事实/修正答案），空串表示不在这些阶段
// attached：已上传但还没发消息的文件（显示成输入区上方的附件条）
// uploading：上传中，禁用上传按钮避免连点产生多个没人用的文件
const props = defineProps<{
  messages: ChatMessage[]; streaming: boolean; thinking: boolean; phase: string
  attached: StoredFile[]; uploading: boolean
}>()
const emit = defineEmits<{
  send: [text: string]; upload: [file: File]; download: [file: StoredFile]
  'open-memory': []; 'open-knowledge': []
}>()

// 输入框草稿
const draft = ref('')

// 提交输入框内容：非空则发出 send，并清空草稿
function submit() {
  const t = draft.value.trim()
  if (!t) return
  emit('send', t)
  draft.value = ''
}

// 一条消息的全部知识库图片：正文里的引用号在这批里查 key（与工具气泡下的缩略图同源）
const imagesOf = (m: ChatMessage) => (m.tools ?? []).flatMap(t => t.images ?? [])

// 一条消息的全部脚本产物（artifacts 字段）：从各工具上摊平，渲染到正文之后的「生成的文件」区块。
// 它是交付物不是过程，不挂在工具气泡里；一个产物只挂在执行它的那个 tool 上
// （后端每次 save 产新 file_id），无需去重
const artifactsOf = (m: ChatMessage) => (m.tools ?? []).flatMap(t => t.artifacts ?? [])

// 委派标题：模型给的短标题，截断兜底（schema 里要求 8 字以内，但不保证它守约）
const TITLE_MAX = 12
const titleOf = (t: ToolEvent) => String(t.args?.title ?? '').slice(0, TITLE_MAX)

// 工具气泡的稳定 key：优先用 tool_call_id（刷新后由 Chat.vue 归一成 id），没有则退回下标。
// 展开态与嵌套都靠它配对，用下标的话子任务插进来会串
const keyOf = (m: ChatMessage, t: ToolEvent, i: number) => `${m.id}-${t.id ?? i}`

// 某次委派派出去的子 Agent 的工具调用：按 parent 归位，所以三个并行子任务各自成组，
// 不受事件到达顺序影响
const childrenOf = (m: ChatMessage, t: ToolEvent) =>
  (m.tools ?? []).filter(c => c.parent != null && c.parent === t.id)

// 工具气泡文案：label 是后端注册工具时给出的中文动作短语（如「查询火车票」），
// 前端统一按执行状态拼「正在{label}… / 已{label}」，无 label 时回退英文名。
// 委派气泡额外带上 title——并行委派时只靠「委派子任务」四个字分不清哪个是哪个
function toolLabel(t: ToolEvent, done: boolean): string {
  const title = t.tool === 'call_sub_agent' ? titleOf(t) : ''
  const name = (t.label || t.tool) + (title ? ` · ${title}` : '')
  return done ? `已${name}` : `${name}…`
}

// 展开的工具气泡 key 集合（`消息id-工具索引`）：**默认收起**，点一下才展开。
// 委派气泡与普通工具气泡共用这一个集合——两边默认态一致（都收起），只是收的东西不同：
// 委派收的是它那段回答（子任务的工具进度挂在回答之下，不参与折叠），普通工具收的是自己的 result。
const expanded = ref<Set<string>>(new Set())
function toggleTool(key: string) {
  const next = new Set(expanded.value)
  next.has(key) ? next.delete(key) : next.add(key)
  expanded.value = next
}

// 有没有可展开的东西：没有就别给手型光标、点了也别切状态，否则「能点但没反应」。
// 委派节点不无条件可点 —— 它的折叠区只装回答，回答还没到就没什么可收的
const canToggle = (t: ToolEvent) => !!(t.result || t.images?.length)

function toggleNode(m: ChatMessage, t: ToolEvent, i: number) {
  if (!canToggle(t)) return
  toggleTool(keyOf(m, t, i))
}

// 节点当前是否展开（决定展开区显示与箭头朝向）
function isOpen(m: ChatMessage, t: ToolEvent, i: number): boolean {
  return expanded.value.has(keyOf(m, t, i))
}

// 文件大小展示：附件条与产物卡片共用
function formatSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

// 隐藏的文件选择框：交给下面的回形针按钮触发
const fileInput = ref<HTMLInputElement | null>(null)

// 选完文件清空 value：同一个文件再选一次也要能触发 change
function pickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const f = input.files?.[0]
  input.value = ''
  if (f) emit('upload', f)
}
</script>

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
/* 用户消息里的附件：点一下按原文件下载回来（和工具产物一样走 blob，<a href> 带不上 Bearer）。
   气泡整体是 pre-wrap，长文件名要自己兜住换行，别撑破气泡 */
.msg__files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.msg__file {
  font-size: 12px;
  cursor: pointer;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 10px;
  max-width: 100%;
  overflow-wrap: anywhere;
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
/* 工具进度：一行简洁文案 + 状态点（执行中闪烁 / 完成变绿），完成态可点击展开 result。
   用 width 而不是 max-width：外层 .msg--assistant 是 align-items:flex-start 的纵向 flex，
   子项宽度默认由内容决定（fit-content）——写成 max-width 时气泡会贴着文案宽度，展开后
   被 result 的长文本撑开，折叠/展开之间宽度跳变几百像素。定宽后宽度与内容无关。 */
.tool-wrap {
  width: 82%;
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
/* 还没有可展开的内容（如工具执行中、无结果无图）：不给手型光标，点了也不会切状态 */
.tool--static {
  cursor: default;
}
.tool__caret {
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
  /* 名称右侧吸收全部空白，把后面的阶段文案与展开箭头一起推到最右（两者都有时也能并排） */
  margin-right: auto;
  /* 气泡现在是定宽，标签（动作 + 委派标题）过长时会溢出；min-width:0 是 flex 里能缩到
     内容宽度以下的前提，否则 overflow 不生效、省略号出不来 */
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 子 Agent 的工具调用：缩进一级 + 左侧竖线，表达「它跑在委派出去的那个子任务里」。
   width 必须回 auto：父级已经是定宽，若这里再取父级的 82%，每深一层就被缩掉一次
   （实测父子宽度比 0.86），子任务反而比父节点还窄。auto 就是填满父级内容盒。 */
.tool-wrap--sub {
  width: auto;
  margin-left: 20px;
  border-left: 2px solid var(--border);
  padding-left: 8px;
}
.tool-wrap--sub .tool {
  padding: 4px 10px;
}
/* 委派节点的阶段文案（整理中/核对事实/修正答案）：贴在节点右侧 */
.tool__phase {
  font-size: 12px;
  color: var(--text-secondary);
  flex-shrink: 0;
  margin-right: 8px;
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
/* 附件条与输入行共用一个顶边框：附件条不存在时不能多出一条线 */
.chat-panel__bottom {
  border-top: 1px solid var(--border);
  background: var(--bg);
}
.chat-panel__attached {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 24px 0;
}
.attach {
  font-size: 12px;
  color: var(--text-secondary);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 10px;
}
/* 原生文件框藏起来，由回形针按钮触发 */
.chat-panel__file {
  display: none;
}
.chat-panel__input {
  padding: 16px 24px 20px;
  display: flex;
  gap: 12px;
  align-items: flex-end;
}
/* 生成的文件：独立交付物区块，挂在正文之后（不跟着工具气泡走） */
.artifacts {
  margin-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.artifacts__title {
  font-size: 12px;
  color: var(--text-secondary);
}
.artifact {
  display: flex;
  align-items: center;
  gap: 8px;
  max-width: 340px;
  padding: 6px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
}
.artifact:hover {
  border-color: var(--accent);
}
.artifact__icon {
  color: var(--text-secondary);
}
.artifact__name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.artifact__size {
  font-size: 12px;
  color: var(--text-secondary);
}
.artifact__action {
  color: var(--accent);
}
.chat-panel__send {
  flex-shrink: 0;
}
.chat-panel__memory {
  flex-shrink: 0;
}
</style>
