<script setup lang="ts">
// TripResultPanel.vue —— 旅行域结果面板（结果渲染抽象里 travel 域对应的组件）
// 顶部是多套方案切换（方案A/B/C），下方是当前方案的行程/预算/天气/地图四个 tab；支持滚动
import { computed, ref, watch } from 'vue'
import TripTimeline from './TripTimeline.vue'
import BudgetPanel from './BudgetPanel.vue'
import WeatherPanel from './WeatherPanel.vue'
import MapView from './MapView.vue'
import type { TripPlanSet, TripPlan } from '@/types'

const props = defineProps<{ planSet: TripPlanSet }>()

// 当前选中的方案索引（默认第一套）
const activePlan = ref(0)
// 当前结果 tab：行程/预算/天气/地图
const activeTab = ref('trip')

// 当前选中的方案对应的完整行程
const currentPlan = computed<TripPlan | null>(() => props.planSet.plans[activePlan.value]?.plan ?? null)

// 切换方案时复位到「行程」tab，避免停留在别的 tab 看到旧数据
watch(activePlan, () => {
  activeTab.value = 'trip'
})
</script>

<template>
  <div class="trip-result">
    <!-- 多套方案切换 -->
    <div class="trip-result__plans">
      <button
        v-for="(p, i) in planSet.plans"
        :key="i"
        class="plan-tab"
        :class="{ 'is-active': activePlan === i }"
        @click="activePlan = i"
      >
        <span class="plan-tab__name">{{ p.name }}</span>
        <span class="plan-tab__focus">{{ p.focus }}</span>
      </button>
    </div>

    <!-- 当前方案的行程/预算/天气/地图 -->
    <el-tabs v-model="activeTab" class="trip-result__tabs" stretch>
      <el-tab-pane label="行程" name="trip">
        <div class="trip-result__scroll">
          <TripTimeline :plan="currentPlan" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="预算" name="budget">
        <div class="trip-result__scroll">
          <BudgetPanel :plan="currentPlan" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="天气" name="weather">
        <div class="trip-result__scroll">
          <WeatherPanel :plan="currentPlan" />
        </div>
      </el-tab-pane>
      <el-tab-pane label="地图" name="map">
        <MapView v-if="activeTab === 'map'" :plan="currentPlan" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.trip-result {
  height: 100%;
  display: flex;
  flex-direction: column;
}
/* 方案切换：横向卡片按钮，选中态琥珀描边 */
.trip-result__plans {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}
.plan-tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--surface);
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, background 0.15s;
}
.plan-tab:hover {
  border-color: var(--accent);
}
.plan-tab.is-active {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.plan-tab__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.plan-tab__focus {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.4;
}
/* tab 内容滚动容器：行程过长时可下滑 */
.trip-result__tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
/* 让 el-tabs 的内容区填满剩余高度，滚动容器才有确定高度可滚 */
.trip-result__tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.trip-result__tabs :deep(.el-tab-pane) {
  height: 100%;
}
.trip-result__scroll {
  height: 100%;
  /* 始终显示滚动条，避免展开/收起时滚动条出现消失导致抖动 */
  overflow-y: scroll;
}
</style>
