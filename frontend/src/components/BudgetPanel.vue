<script setup lang="ts">
// BudgetPanel.vue —— 预算面板：用 el-table 展示预算明细（住宿/餐饮/门票/总计）
import { computed } from 'vue'
import type { TripPlan } from '@/types'

const props = defineProps<{ plan: TripPlan | null }>()

// 把后端 budget 字段转成表格行，总计行高亮
const rows = computed(() => {
  const b = props.plan?.budget
  if (!b) return []
  return [
    { category: '交通', amount: b.total_transportation ?? 0 },
    { category: '住宿', amount: b.total_hotels },
    { category: '餐饮', amount: b.total_meals },
    { category: '景点门票', amount: b.total_attractions },
    { category: '总计', amount: b.total, isTotal: true },
  ]
})

// 金额格式化
function fmt(v: number): string {
  return `¥${v}`
}
</script>

<template>
  <div class="budget">
    <!-- 空态 -->
    <p v-if="!rows.length" class="budget__empty">暂无预算数据</p>

    <el-table v-else :data="rows" size="small" :show-header="false" class="budget__table">
      <el-table-column prop="category" label="类别" />
      <el-table-column label="金额" align="right">
        <template #default="{ row }">
          <span :class="{ 'budget__total': row.isTotal }">{{ fmt(row.amount) }}</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped>
.budget {
  font-size: 14px;
}
.budget__empty {
  color: var(--text-secondary);
  text-align: center;
  padding: 32px 0;
}
.budget__total {
  font-weight: 700;
  color: var(--accent);
}
</style>
