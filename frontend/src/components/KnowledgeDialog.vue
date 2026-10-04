<!-- components/KnowledgeDialog.vue —— 知识库弹窗：添加资料（粘贴/文件/网址）+ 已入库列表 -->
<template>
  <el-dialog v-model="visible" title="知识库" width="680px">
    <el-tabs v-model="tab">
      <el-tab-pane label="添加资料" name="add">
        <el-radio-group v-model="mode" class="kb-mode">
          <el-radio-button value="text">粘贴文本</el-radio-button>
          <el-radio-button value="file">上传文件</el-radio-button>
          <el-radio-button value="url">抓取网址</el-radio-button>
        </el-radio-group>

        <template v-if="mode === 'text'">
          <el-input v-model="title" class="kb-field" placeholder="标题（可选，不填取正文开头）" maxlength="60" />
          <el-input v-model="content" type="textarea" :rows="8" placeholder="粘贴攻略正文…"/>
        </template>

        <template v-else-if="mode === 'file'">
          <el-upload :auto-upload="false" :show-file-list="false" accept=".txt,.md,.markdown" :on-change="onFileChange">
            <el-button>选择文件</el-button>
          </el-upload>
          <p v-if="fileName" class="kb-tip">已读取：{{ fileName }}（{{ content.length }} 字）</p>
          <p v-else class="kb-tip">支持 .txt / .md，内容在浏览器里读成文本后入库</p>
        </template>

        <template v-else>
          <el-input v-model="url" placeholder="https://…（攻略页地址）"/>
          <p class="kb-tip">用无头浏览器打开并跑完 JS 再取正文，需要几秒到十几秒；页面里的插图会转成文字描述一起入库</p>
        </template>

        <div class="kb-actions">
          <el-button type="primary" :loading="saving" :disabled="!canSubmit" @click="submit">入库</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="`已入库（${docs.length}）`" name="list">
        <p v-if="!docs.length" class="kb-empty">知识库还是空的，先添加一些攻略资料</p>
        <div v-else class="kb-docs">
          <div v-for="d in docs" :key="d.source_url || d.title" class="kb-doc">
            <div class="kb-doc__main">
              <span class="kb-doc__title">{{ d.title }}</span>
              <span class="kb-doc__meta">{{ d.chunks }} 个片段</span>
            </div>
            <KnowledgeThumbs :images="thumbs(d)" :size="40" />
            <el-button link type="danger" @click="remove(d)">删除</el-button>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-dialog>
</template>

<script setup lang="ts">
import {computed, ref, watch} from 'vue'
import {ElMessage, ElMessageBox, type UploadFile} from 'element-plus'
import {deleteDocument, ingestText, ingestUrl, listDocuments} from '@/services/api'
import KnowledgeThumbs from '@/components/KnowledgeThumbs.vue'

interface DocItem {
  title: string
  source_url: string | null
  chunks: number
  // 该文档收录的图片对象 key 列表（后端已按 key 去重）
  image_keys: string[]
}

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean] }>()
const visible = computed({get: () => props.modelValue, set: v => emit('update:modelValue', v)})

const tab = ref('add')
const mode = ref<'text' | 'file' | 'url'>('text')
const title = ref('')
const content = ref('')
const url = ref('')
const saving = ref(false)
const docs = ref<DocItem[]>([])
// 已读取的文件名：不用 el-upload 自带的文件列表，因为它带 limit 限制，选错文件后换不了
const fileName = ref('')

watch(visible, v => { if (v) loadDocs() })

async function loadDocs() {
  try { docs.value = await listDocuments() } catch { docs.value = [] }
}

// 文件走「粘贴文本」同一条路：在浏览器里读成文本再提交，后端不需要处理 multipart
async function onFileChange(uploadFile: UploadFile) {
  const raw = uploadFile.raw
  if (!raw) return
  content.value = await raw.text()
  fileName.value = raw.name
  if (!title.value) title.value = raw.name.replace(/\.[^.]+$/, '')
}

const canSubmit = computed(() => (mode.value === 'url' ? !!url.value.trim() : !!content.value.trim()))

// 列表只有 key 没有 desc，补一个空 desc 凑成缩略图组件要的形状
const thumbs = (d: DocItem) => (d.image_keys || []).map(key => ({key, desc: ''}))

async function submit() {
  saving.value = true
  try {
    const res = mode.value === 'url'
      ? await ingestUrl(url.value.trim())
      : await ingestText(content.value, title.value.trim())
    ElMessage.success(`已入库 ${res.chunks} 个片段`)
    content.value = ''
    url.value = ''
    title.value = ''
    fileName.value = ''
    await loadDocs()
    tab.value = 'list'
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '入库失败')
  } finally {
    saving.value = false
  }
}

async function remove(doc: DocItem) {
  if (!doc.source_url) return
  try {
    await ElMessageBox.confirm(`删除「${doc.title}」的全部 ${doc.chunks} 个片段？`, '确认删除', {type: 'warning'})
  } catch {
    return
  }
  try {
    const res = await deleteDocument(doc.source_url)
    ElMessage.success(`已删除 ${res.deleted} 个片段`)
    await loadDocs()
  } catch {
    ElMessage.error('删除失败')
  }
}
</script>

<style scoped>
.kb-mode {
  margin-bottom: 14px;
}

.kb-field {
  margin-bottom: 10px;
}

.kb-tip {
  margin: 8px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
}

.kb-actions {
  margin-top: 16px;
  text-align: right;
}

.kb-empty {
  margin: 0;
  padding: 24px 0;
  text-align: center;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.kb-docs {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 420px;
  overflow-y: auto;
}

.kb-doc {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--el-fill-color-light);
}

.kb-doc__main {
  display: flex;
  flex-direction: column;
  gap: 3px;
  flex: 1;
  min-width: 0;
}

/* 缩略图在行内与文字居中对齐：组件自带的 margin-top 是给聊天气泡里的堆叠留的间距 */
.kb-doc :deep(.thumbs) {
  margin-top: 0;
}

.kb-doc__title {
  font-size: 14px;
  color: var(--el-text-color-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.kb-doc__meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
