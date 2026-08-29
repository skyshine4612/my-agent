<script setup lang="ts">
// ProgressPanel.vue —— 执行进度面板：把「子任务状态」与「思考过程」合并展示
// 每个子 Agent 一行，左侧是状态（完成/进行中/等待），下方缩进展示该 Agent 的工具调用流
import { computed } from 'vue'
import { useTripStore } from '@/stores/trip'
import type { SubTask, PlanEvent } from '@/types'

const tripStore = useTripStore()

// 从 plan_start 事件中取出子任务 DAG（尚未收到时为空数组 → 展示空态）
const tasks = computed<SubTask[]>(() => {
  const e = tripStore.events.find((ev) => ev.type === 'plan_start')
  return (e?.tasks as SubTask[] | undefined) ?? []
})

// 汇总各子任务实时状态：task_start→running、task_done→done
const status = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const ev of tripStore.events) {
    if (ev.type === 'task_start') map[ev.task_id] = 'running'
    else if (ev.type === 'task_done') map[ev.task_id] = 'done'
  }
  return map
})

// 已完成子任务数量
const doneCount = computed(() => Object.values(status.value).filter((s) => s === 'done').length)

// 按依赖拓扑分层：同一层无依赖可并行，渲染出 DAG 的分层结构
const layers = computed<SubTask[][]>(() => {
  const out: SubTask[][] = []
  const done = new Set<string>()
  while (done.size < tasks.value.length) {
    const layer = tasks.value.filter(
      (t) => !done.has(t.id) && t.depends_on.every((d) => done.has(d)),
    )
    if (!layer.length) break
    out.push(layer)
    layer.forEach((t) => done.add(t.id))
  }
  return out
})

// 每个 Agent 的工具调用流：把 SSE 的 tool_call / tool_result 事件按 agent 分组，
// 供每个子任务行下方缩进展示（key 是 agent 名，与 tasks 的 agent 字段对应）
const agentSteps = computed<Map<string, PlanEvent[]>>(() => {
  const map = new Map<string, PlanEvent[]>()
  for (const ev of tripStore.events) {
    if (ev.type === 'tool_call' || ev.type === 'tool_result') {
      const agent = (ev.agent as string) || '未知'
      if (!map.has(agent)) map.set(agent, [])
      map.get(agent)!.push(ev)
    }
  }
  return map
})

// 是否进入「生成方案」阶段（搜索子任务都完成后、最终结果前的整合阶段）
const generating = computed(() => tripStore.events.some((e) => e.type === 'generating'))

// 工具名 → 更友好的中文描述
const TOOL_LABELS: Record<string, string> = {
  poi_search: '搜索 POI',
  weather_query: '查询天气',
  route_plan: '规划路线',
  budget_calc: '核算预算',
}

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name
}

// el-tag 的类型必须是字面量联合，避免模板绑定 string 报类型错误
type TagType = 'success' | 'warning' | 'info' | 'primary' | 'danger'
function statusInfo(s?: string): { text: string; type: TagType } {
  if (s === 'done') return { text: '完成', type: 'success' }
  if (s === 'running') return { text: '进行中', type: 'warning' }
  return { text: '等待', type: 'info' }
}
</script>

<template>
  <div class="progress">
    <div class="progress__head">
      <span class="progress__title">执行进度</span>
      <span v-if="layers.length" class="progress__meta">
        {{ doneCount }} / {{ tasks.length }} 完成
      </span>
    </div>

    <!-- 尚未收到 plan_start：等待拆解任务的空态 -->
    <p v-if="!layers.length" class="progress__empty">正在拆解任务…</p>

    <!-- 按层渲染子任务，每个子任务行 = 状态 + 工具调用流 -->
    <div v-else class="progress__layers">
      <div v-for="(layer, li) in layers" :key="li" class="progress__layer">
        <div
          v-for="t in layer"
          :key="t.id"
          class="progress__task"
          :class="{ 'is-running': status[t.id] === 'running' }"
        >
          <div class="progress__task-head">
            <span class="progress__task-agent">{{ t.agent }}</span>
            <el-tag size="small" :type="statusInfo(status[t.id]).type" effect="light">
              {{ statusInfo(status[t.id]).text }}
            </el-tag>
          </div>

          <!-- 该 Agent 的工具调用流（缩进展示，与状态合并到同一行块内） -->
          <div v-if="(agentSteps.get(t.agent) ?? []).length" class="progress__steps">
            <div v-for="(s, si) in agentSteps.get(t.agent)!" :key="si" class="progress__step">
              <template v-if="s.type === 'tool_call'">
                <span class="progress__step-icon">⚙</span>
                <span class="progress__step-text">调用 {{ toolLabel(s.tool) }}</span>
              </template>
              <template v-else-if="s.type === 'tool_result'">
                <span class="progress__step-icon progress__step-icon--ok">✓</span>
                <span class="progress__step-text progress__step-text--muted">已获取结果</span>
              </template>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 所有子任务完成后：生成方案的整合阶段 -->
    <div v-if="generating" class="progress__generating">
      <span class="progress__gen-dot"></span>
      正在生成方案…
    </div>
  </div>
</template>

<style scoped>
.progress {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  background: var(--surface);
  width: 100%;   /* 占满消息区宽度：编排进度 + 思考过程内容多，需要足够宽 */
}
.progress__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.progress__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--accent);
}
.progress__meta {
  font-size: 12px;
  color: var(--text-secondary);
}
.progress__generating {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
  font-size: 13px;
}
.progress__gen-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  animation: genpulse 1.2s infinite ease-in-out;
}
@keyframes genpulse {
  0%,
  100% {
    opacity: 0.4;
  }
  50% {
    opacity: 1;
  }
}
.progress__empty {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
}
.progress__layers {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.progress__layer {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.progress__task {
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--bg);
  transition: background 0.15s;
}
.progress__task.is-running {
  background: var(--accent-soft);
}
.progress__task-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.progress__task-agent {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.progress__steps {
  margin-top: 6px;
  margin-left: 4px;
  padding-left: 10px;
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.progress__step {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
}
.progress__step-icon {
  color: var(--accent);
  flex-shrink: 0;
}
.progress__step-icon--ok {
  color: #16a34a;
}
.progress__step-text {
  color: var(--text);
}
.progress__step-text--muted {
  color: var(--text-secondary);
}
</style>
