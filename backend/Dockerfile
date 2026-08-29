FROM python:3.11-slim
WORKDIR /app
# 安装依赖（与 pyproject.toml 保持一致）
RUN pip install --no-cache-dir \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.32.0" "pydantic>=2.0.0" \
    "pydantic-settings>=2.0.0" "openai>=1.0.0" "mcp>=1.0.0" "httpx>=0.27.0" \
    "sse-starlette>=2.0.0" "python-dotenv>=1.0.0"
COPY app ./app
EXPOSE 8000
# DB_PATH 由 compose 注入，默认落到挂载卷 /app/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
