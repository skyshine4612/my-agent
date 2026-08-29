// stores/trip.ts —— 行程结果与编排进度状态
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { TripPlanSet, PlanEvent } from '@/types'

export const useTripStore = defineStore('trip', () => {
  // 多套行程方案（后端 final_result 返回的 plans 数组）
  const planSet = ref<TripPlanSet | null>(null)
  // 编排进度事件流（SSE 推送的全部中间事件，含思考过程）
  const events = ref<PlanEvent[]>([])
  return { planSet, events }
})
