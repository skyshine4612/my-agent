FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
# 构建期要从 PyPI 拉 uv 本身与其展开出的依赖；直连 pypi.org 不通的机器上构建会直接失败，索引可换
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv \
 && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt \
 && pip uninstall -y uv
COPY app ./app
EXPOSE 8000
# 数据库与 JWT 密钥由挂载的 /app/.env 提供（SUPABASE_DATABASE_URL / SUPABASE_JWT_SECRET）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
