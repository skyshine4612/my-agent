<!-- components/MessageMarkdown.vue —— 回答正文：markdown 段 + 知识库图片块（图文混排） -->
<script setup lang="ts">
import { computed, watch } from 'vue'
import { useImageUrls } from '@/services/imageUrls'
import { buildBlocks, type Block } from '@/services/markdownBlocks'
import type { KnowledgeImage } from '@/types'

const props = defineProps<{ content: string; images?: KnowledgeImage[] }>()

const { urls, hold, release } = useImageUrls()

// 引用号 → Storage 对象 key。号是持久的（chunk 行 id - 块内下标），同一轮内不会撞。
const keyOf = computed(() => {
  const m = new Map<string, string>()
  for (const i of props.images ?? []) if (i.ref && i.key) m.set(i.ref, i.key)
  return m
})

// 持有与释放只发生在**消息这一层**：块数组每次随 token 重算都不会卸载这个持有者，
// 所以不会出现「块组件重建 → release → refs 归零 → revoke → 重新 fetch」的闪烁链。
watch(() => [...keyOf.value.values()], (next, prev) => {
  const keep = new Set(next)
  for (const k of prev ?? []) if (!keep.has(k)) release(k)
  for (const k of next) hold(k)
}, { immediate: true })

const blocks = computed(() => buildBlocks(props.content))

// 取不到图（号不存在 / 图已删 / blob 还没到位）时给空串，模板据此跳过整块 ——
// 宁可不显示，也不要露出裸标记或裂图
function imgSrc(b: Block): string {
  if (b.kind !== 'img') return ''
  const key = keyOf.value.get(b.ref)
  return (key ? urls.value[key] : undefined) ?? ''
}

const previewList = computed(() => blocks.value.map(imgSrc).filter(Boolean))
</script>

<template>
  <template v-for="b in blocks" :key="b.key">
    <div v-if="b.kind === 'html'" v-html="b.html"></div>
    <el-image v-else-if="imgSrc(b)" :src="imgSrc(b)"
              :preview-src-list="previewList" :initial-index="previewList.indexOf(imgSrc(b))"
              fit="contain" class="kb-inline">
      <!-- 文档已删的图会 404：error 槽吞掉，别裂图刷屏 -->
      <template #error><div class="kb-inline__fail">图已失效</div></template>
    </el-image>
  </template>
</template>

<style scoped>
.kb-inline {
  display: block;
  max-width: 100%;
  margin: 10px 0;
  border-radius: 8px;
  cursor: zoom-in;
  border: 1px solid var(--border);
}

.kb-inline__fail {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 80px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  background: var(--bg);
}
</style>
