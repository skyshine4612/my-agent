// types/index.ts —— 全局共享的 TypeScript 类型定义

// 经纬度坐标点
export interface GeoPoint { lng: number; lat: number }

// 景点
export interface Attraction {
  name: string
  location: GeoPoint | null
  price: number
  photo: string
  visit_duration?: number
  description?: string
  transit?: string
}

// 酒店
export interface Hotel {
  name: string
  price: number
  photo: string
  address?: string
  description?: string
}

// 一餐（早餐/午餐/晚餐）
export interface Meal {
  type: string
  name: string
  dish?: string
  description?: string
  cost: number
}

// 一天的行程
export interface DayPlan {
  date: string
  title?: string
  description?: string
  attractions: Attraction[]
  hotel: Hotel | null
  meals: Meal[]
}

// 天气信息
export interface WeatherInfo {
  date: string
  day_weather: string
  day_temp: number
  night_temp: number
}

// 预算明细
export interface Budget {
  total_transportation: number
  total_hotels: number
  total_meals: number
  total_attractions: number
  total: number
}

// 单个完整行程计划
export interface TripPlan {
  city: string
  days: DayPlan[]
  weather_info: WeatherInfo[]
  budget: Budget
  overall_suggestions: string
}

// 一套可选方案（名称 + 侧重说明 + 完整行程）
export interface PlanOption {
  name: string
  focus: string
  plan: TripPlan
}

// 多套方案：后端最终返回 plans 数组
export interface TripPlanSet {
  plans: PlanOption[]
}

// 编排进度事件：SSE 流中逐条推送的中间结果
export interface PlanEvent { type: string; [k: string]: any }

// 编排子任务：plan_start 事件里 Planner 拆解出的子任务（含依赖关系）
export interface SubTask {
  id: string
  agent: string
  params: Record<string, any>
  depends_on: string[]
}

// 对话消息：前端聊天区的渲染单元，按 role/kind 区分用户、文本回复、澄清提问、行程结果
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  kind: 'text' | 'clarify' | 'result'
  content?: string
  questions?: string[]
  planSet?: TripPlanSet
}
