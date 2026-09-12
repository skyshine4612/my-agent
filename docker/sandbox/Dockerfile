# 沙箱镜像：依赖清单就是脚本的可用能力边界，声明在 pyproject.toml 里（唯一一处）。
# 不做「按需装 PyPI 包」：镜像无网，且动态装包等于把「什么代码能进系统」的决定权从白名单手里拿走。
FROM python:3.11-slim

WORKDIR /app
# 构建期要从 PyPI 拉两样东西：uv 本身，和它展开出来的依赖。写死官方源的话，
# 在直连 pypi.org 不通的机器上构建直接失败（from versions: none），所以索引可换。
# 用 ARG 包在 RUN 里而不是 ENV：它不该留在运行时镜像里，沙箱的 env 要尽量干净。
ARG PIP_INDEX_URL=https://pypi.org/simple
# 从 uv.lock 装而不是照抄一份包名：清单只有一个来源，且版本锁死、构建可复现
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir -i "$PIP_INDEX_URL" uv \
 && uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
 && pip install --no-cache-dir -i "$PIP_INDEX_URL" -r /tmp/requirements.txt \
 && pip uninstall -y uv

ENV PYTHONDONTWRITEBYTECODE=1

COPY app ./app

# 非 root：容器内提权是这条链路上 cap_drop 之外还需要用户态兜住的部分
RUN useradd --create-home --uid 10001 sandbox
USER sandbox

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
