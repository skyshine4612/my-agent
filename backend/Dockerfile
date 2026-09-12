FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
 && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip uninstall -y uv
COPY app ./app
EXPOSE 8000
# 数据库与 JWT 密钥由挂载的 /app/.env 提供（SUPABASE_DATABASE_URL / SUPABASE_JWT_SECRET）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
