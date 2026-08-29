<script setup lang="ts">
// WeatherPanel.vue —— 天气面板：用 el-card 逐日展示天气/气温
import { computed } from 'vue'
import type { TripPlan } from '@/types'

const props = defineProps<{ plan: TripPlan | null }>()

// 天气信息列表：空结果时展示空态
const weather = computed(() => props.plan?.weather_info ?? [])
</script>

<template>
  <div class="weather">
    <!-- 空态 -->
    <p v-if="!weather.length" class="weather__empty">暂无天气数据</p>

    <div v-else class="weather__grid">
      <el-card v-for="(w, i) in weather" :key="i" shadow="never" class="weather__card">
        <div class="weather__date">{{ w.date }}</div>
        <div class="weather__desc">{{ w.day_weather }}</div>
        <div class="weather__temp">
          <span class="weather__high">{{ w.day_temp }}°</span>
          <span class="weather__slash">/</span>
          <span class="weather__low">{{ w.night_temp }}°</span>
        </div>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
.weather {
  font-size: 14px;
}
.weather__empty {
  color: var(--text-secondary);
  text-align: center;
  padding: 32px 0;
}
.weather__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 12px;
}
.weather__card {
  border: 1px solid var(--border);
  border-radius: 10px;
}
.weather__date {
  color: var(--text-secondary);
  font-size: 13px;
  margin-bottom: 8px;
}
.weather__desc {
  font-weight: 600;
  margin-bottom: 6px;
}
.weather__temp {
  display: flex;
  align-items: baseline;
  gap: 4px;
}
.weather__high {
  font-size: 22px;
  font-weight: 700;
  color: var(--accent);
}
.weather__slash {
  color: var(--text-secondary);
}
.weather__low {
  color: var(--text-secondary);
}
</style>
