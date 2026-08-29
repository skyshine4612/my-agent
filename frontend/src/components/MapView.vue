<script setup lang="ts">
// MapView.vue —— 高德地图：标记景点、按天绘制路线并自适应视野；无 JS key 或加载失败时降级为景点列表
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { load as loadAMap } from '@amap/amap-jsapi-loader'
import type { TripPlan } from '@/types'

const props = defineProps<{ plan: TripPlan | null }>()

// 地图容器 DOM
const container = ref<HTMLElement | null>(null)
// 无 key / 加载失败时降级为列表展示，绝不抛错
const fallback = ref(false)
// 高德地图实例（运行时才创建，类型为 any）
let map: any = null

// 高德 JS API key：从环境变量读取，未配置则走降级分支
const amapKey = import.meta.env.VITE_AMAP_WEB_JS_KEY as string | undefined

// 有坐标的景点（location 可能为 null，需过滤），附天数用于按天分组
const points = computed(() => {
  const pts: { name: string; lng: number; lat: number; day: number }[] = []
  props.plan?.days.forEach((d, i) => {
    d.attractions.forEach((a) => {
      if (a.location) pts.push({ name: a.name, lng: a.location.lng, lat: a.location.lat, day: i + 1 })
    })
  })
  return pts
})

// 按天分组景点（保持天序），用于绘制每天的 Polyline
function groupByDay() {
  const groups = new Map<number, { name: string; lng: number; lat: number; day: number }[]>()
  for (const p of points.value) {
    if (!groups.has(p.day)) groups.set(p.day, [])
    groups.get(p.day)!.push(p)
  }
  return [...groups.values()]
}

onMounted(async () => {
  // 无 key 或容器未就绪 → 降级为列表，不报错
  if (!amapKey || !container.value) {
    fallback.value = true
    return
  }
  try {
    // 加载高德 JSAPI v2.0
    const AMap = await loadAMap({ key: amapKey, version: '2.0', plugins: [] })
    map = new AMap.Map(container.value, { zoom: 11, resizeEnable: true })
    const overlays: any[] = []

    // 逐景点添加 marker（位置用 [lng, lat] 顺序），label 显示游览序号（1、2、3…）
    points.value.forEach((p, i) => {
      overlays.push(
        new AMap.Marker({
          position: [p.lng, p.lat],
          title: p.name,
          label: { content: String(i + 1), offset: new AMap.Pixel(0, -18) },
        }),
      )
    })

    // 按天绘制折线：同一天内相邻景点连成一条线
    for (const dayPts of groupByDay()) {
      if (dayPts.length > 1) {
        overlays.push(
          new AMap.Polyline({
            path: dayPts.map((p) => [p.lng, p.lat]),
            // strokeColor 用琥珀 --accent（#d97706）；地图 canvas 绘制不便引用 CSS 变量，直接写色值
            strokeColor: '#d97706',
            strokeWeight: 3,
            strokeOpacity: 0.6,
          }),
        )
      }
    }

    // 统一添加覆盖物并自适应视野
    if (overlays.length) {
      map.add(overlays)
      map.setFitView(overlays)
    }
  } catch {
    // 加载/初始化失败同样降级，保证界面可用
    fallback.value = true
  }
})

onBeforeUnmount(() => {
  // 销毁地图实例，避免内存泄漏
  if (map) {
    map.destroy()
    map = null
  }
})
</script>

<template>
  <div class="map-view">
    <!-- 有 key：渲染地图画布 -->
    <div v-if="!fallback" ref="container" class="map-view__canvas"></div>

    <!-- 降级：景点坐标列表 -->
    <ul v-if="fallback" class="map-view__fallback">
      <li v-for="(p, i) in points" :key="i" class="map-view__item">
        <span class="map-view__day">D{{ p.day }}</span>
        <span class="map-view__name">{{ p.name }}</span>
        <span class="map-view__coord">{{ p.lng.toFixed(3) }}, {{ p.lat.toFixed(3) }}</span>
      </li>
      <li v-if="!points.length" class="map-view__none">暂无景点坐标</li>
    </ul>
  </div>
</template>

<style scoped>
.map-view {
  width: 100%;
}
.map-view__canvas {
  width: 100%;
  height: 420px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.map-view__fallback {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.map-view__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  font-size: 14px;
}
.map-view__day {
  flex-shrink: 0;
  font-weight: 600;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 12px;
}
.map-view__name {
  flex: 1;
}
.map-view__coord {
  color: var(--text-secondary);
  font-size: 12px;
}
.map-view__none {
  color: var(--text-secondary);
  text-align: center;
  padding: 24px 0;
}
</style>
