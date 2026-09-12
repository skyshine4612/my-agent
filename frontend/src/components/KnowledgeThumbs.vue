<!-- components/KnowledgeThumbs.vue —— 知识库图片缩略图：点击放大，已删的图静默 -->
<script setup lang="ts">
import {computed, ref, watch} from 'vue'
import {useImageUrls} from '@/services/imageUrls'
import type {KnowledgeImage} from '@/types'

const props = withDefaults(defineProps<{ images: KnowledgeImage[], size?: number }>(), {size: 56})

const {urls, hold, release} = useImageUrls()
// 只认拿到 key 的图：key 为 null 的是「上传失败」，这时界面上本来就该什么都没有
const keys = computed(() => (props.images || []).map(i => i.key).filter((k): k is string => !!k))
const shown = ref<string[]>([])

// 增删都在这里做：URL 的诞生与回收不进渲染路径
watch(keys, (next, prev) => {
    const keep = new Set(next)
    for (const k of prev || []) if (!keep.has(k)) { release(k); shown.value = shown.value.filter(x => x !== k) }
    for (const k of next) if (!shown.value.includes(k)) { shown.value.push(k); hold(k) }
}, {immediate: true})

const previewList = computed(() => shown.value.map(k => urls.value[k]).filter(Boolean))
</script>

<template>
  <div v-if="shown.length" class="thumbs">
    <el-image v-for="(k, i) in shown" :key="k" :src="urls[k]"
              :preview-src-list="previewList" :initial-index="i"
              :style="{width: `${size}px`, height: `${size}px`}"
              fit="cover" class="thumbs__item">
      <!-- 已删文档的图会 404：error 槽吞掉，别裂图刷屏 -->
      <template #error><div class="thumbs__fail">图已失效</div></template>
    </el-image>
  </div>
</template>

<style scoped>
.thumbs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}

.thumbs__item {
  border-radius: 8px;
  cursor: zoom-in;
  border: 1px solid var(--border);
}

.thumbs__fail {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  background: var(--bg);
}
</style>
