<script setup lang="ts">
// TripTimeline.vue —— 行程时间线：按天折叠展示每天的标题/描述/景点/酒店/餐食，
// 景点与酒店展示缩略图与介绍
import { ref, computed } from 'vue'
import type { TripPlan, DayPlan } from '@/types'

const props = defineProps<{ plan: TripPlan | null }>()

// 折叠面板：默认展开第一天
const activeDays = ref<string[]>(['0'])

// 天数列表：空结果时渲染空态
const days = computed(() => props.plan?.days ?? [])

// 金额展示：0 或空显示「免费」，否则带 ¥ 前缀
function money(v: number): string {
  return v ? `¥${v}` : '免费'
}

// 把某天的景点/餐食/酒店按时间顺序（早餐→景点→午餐→景点→晚餐→酒店）排列成事件列表
function dayTimeline(day: DayPlan) {
  type Item = { kind: 'attraction' | 'hotel' | 'meal'; data: any }
  const items: Item[] = []
  const breakfast = day.meals.find((m) => m.type.includes('早'))
  const lunch = day.meals.find((m) => m.type.includes('午'))
  const dinner = day.meals.find((m) => m.type.includes('晚'))
  if (breakfast) items.push({ kind: 'meal', data: breakfast })
  day.attractions.forEach((a, i) => {
    items.push({ kind: 'attraction', data: a })
    if (i === 0 && lunch) items.push({ kind: 'meal', data: lunch })
  })
  if (dinner) items.push({ kind: 'meal', data: dinner })
  if (day.hotel) items.push({ kind: 'hotel', data: day.hotel })
  return items
}

// 图片加载失败时隐藏（某些 URL 可能失效）
function onImgError(e: Event) {
  ;(e.target as HTMLImageElement).style.display = 'none'
}
</script>

<template>
  <div class="trip">
    <!-- 空态：尚未生成行程 -->
    <p v-if="!days.length" class="trip__empty">暂无行程结果</p>

    <template v-else>
      <!-- 整体建议：以低调的一行放最前 -->
      <p v-if="props.plan?.overall_suggestions" class="trip__suggest">
        {{ props.plan.overall_suggestions }}
      </p>

      <el-collapse v-model="activeDays">
        <el-collapse-item v-for="(day, i) in days" :key="i" :name="String(i)">
          <template #title>
            <span class="trip__day-title">{{ day.title || `第 ${i + 1} 天` }}</span>
            <span class="trip__day-date">{{ day.date }}</span>
          </template>

          <!-- 当天概述 -->
          <p v-if="day.description" class="trip__day-desc">{{ day.description }}</p>

          <el-timeline class="trip__timeline">
            <el-timeline-item
              v-for="(item, ii) in dayTimeline(day)"
              :key="ii"
              :timestamp="money(item.data.price ?? item.data.cost)"
              :color="item.kind === 'attraction' ? 'var(--accent)' : item.kind === 'hotel' ? 'var(--accent-strong)' : 'var(--text-tertiary)'"
              placement="top"
            >
              <!-- 景点：缩略图 + 名称/介绍 + 编辑/删除 -->
              <div v-if="item.kind === 'attraction'" class="trip__poi">
                <img v-if="item.data.photo" class="trip__thumb" :src="item.data.photo" loading="lazy" @error="onImgError" alt="" />
                <div class="trip__poi-main">
                  <div class="trip__poi-head">
                    <span class="trip__poi-name">{{ item.data.name }}</span>
                  </div>
                  <span v-if="item.data.transit" class="trip__transit">{{ item.data.transit }}</span>
                  <p v-if="item.data.description" class="trip__poi-desc">{{ item.data.description }}</p>
                </div>
              </div>

              <!-- 酒店 -->
              <div v-else-if="item.kind === 'hotel'" class="trip__poi">
                <img v-if="item.data.photo" class="trip__thumb" :src="item.data.photo" loading="lazy" @error="onImgError" alt="" />
                <div class="trip__poi-main">
                  <div class="trip__poi-head">
                    <span class="trip__stay">入住 · {{ item.data.name }}</span>
                  </div>
                  <p v-if="item.data.description" class="trip__poi-desc">{{ item.data.description }}</p>
                </div>
              </div>

              <!-- 餐食：店铺图 + 店名 + 推荐菜品 -->
              <div v-else class="trip__poi">
                <img v-if="item.data.photo" class="trip__thumb" :src="item.data.photo" loading="lazy" @error="onImgError" alt="" />
                <div class="trip__poi-main">
                  <span class="trip__meal">{{ item.data.type }} · {{ item.data.name }}</span>
                  <span v-if="item.data.dish" class="trip__dish">推荐：{{ item.data.dish }}</span>
                  <p v-if="item.data.description" class="trip__poi-desc">{{ item.data.description }}</p>
                </div>
              </div>
            </el-timeline-item>
          </el-timeline>
        </el-collapse-item>
      </el-collapse>
    </template>
  </div>
</template>

<style scoped>
.trip {
  font-size: 14px;
}
.trip__empty {
  color: var(--text-secondary);
  text-align: center;
  padding: 32px 0;
}
.trip__suggest {
  margin: 0 0 14px;
  color: var(--text-secondary);
  line-height: 1.7;
  font-size: 13px;
}
.trip__day-title {
  font-weight: 600;
  margin-right: 10px;
}
.trip__day-date {
  color: var(--text-secondary);
  font-size: 13px;
}
.trip__day-desc {
  margin: 0 0 14px;
  color: var(--text-secondary);
  line-height: 1.7;
  font-size: 13px;
}
.trip__timeline {
  padding-left: 4px;
}
.trip__poi {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.trip__thumb {
  width: 60px;
  height: 60px;
  border-radius: 8px;
  object-fit: cover;
  flex-shrink: 0;
  border: 1px solid var(--border);
}
.trip__poi-main {
  flex: 1;
  min-width: 0;
}
.trip__poi-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.trip__poi-name {
  font-weight: 500;
}
.trip__poi-actions {
  display: flex;
  flex-shrink: 0;
  opacity: 0.4;
  transition: opacity 0.15s;
}
.trip__poi:hover .trip__poi-actions {
  opacity: 1;
}
.trip__poi-desc {
  margin: 4px 0 0;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.trip__stay,
.trip__meal {
  color: var(--text);
}
.trip__dish {
  display: block;
  margin-top: 3px;
  color: var(--accent);
  font-size: 12px;
}
.trip__transit {
  display: block;
  margin-top: 4px;
  color: var(--accent);
  font-size: 12px;
}
</style>
