# sandbox/app/main.py
# 沙箱 HTTP 入口：把请求交给 executor，把结果原样回传。
# 不做鉴权（compose 的 internal 网络只有后端进得来）、不记录日志、不留任何状态。
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.executor import run_script

# /tmp 是容器里唯一的可写处（tmpfs），只读根 + 无挂载在 compose 侧保证
WORK_ROOT = Path("/tmp")

app = FastAPI()


class RunIn(BaseModel):
    """执行请求：脚本源码 + 参数 + 输入文件（base64）。"""

    script: str = Field(min_length=1)
    args: dict = Field(default_factory=dict)
    files: list[dict] = Field(default_factory=list)
    timeout: int = 60


@app.get("/health")
async def health():
    """健康探针：compose healthcheck 用。不碰磁盘、不碰子进程。"""
    return {"ok": True}


@app.post("/run")
def run(body: RunIn):
    """同步 def：FastAPI 会丢进线程池，不阻塞事件循环（executor 是阻塞的 subprocess）。"""
    return run_script(body.script, body.args, body.files, body.timeout, WORK_ROOT)
