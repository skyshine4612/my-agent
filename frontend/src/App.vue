<script setup lang="ts">
// App.vue —— 应用根组件：只承载路由出口，全局基础样式与配色在此注入
</script>

<template>
  <router-view />
</template>

<style>
/* 全局基础样式：暖白底、无默认边距，让对话内容成为界面主角 */
* {
  box-sizing: border-box;
}
html,
body,
#app {
  height: 100%;
  margin: 0;
}
body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
    'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}
/* 注：Element Plus 主色（--el-color-primary 系列）统一在 styles/theme.css 中定义，
   组件样式里不再硬编码颜色，避免颜色分散 */
</style>
