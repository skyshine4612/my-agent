# 构建阶段
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci 严格按 lock 文件装：npm install 会在 lock 与 package.json 不一致时静默改写 lock，构建产物不可复现
RUN npm ci
# .env 必须在这里被复制：VITE_ 前缀的变量在 build 阶段内联进产物，晚于本步骤就没有 .env 可读
COPY . .
RUN npm run build

# 运行阶段：nginx 托管静态 + 反代
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
