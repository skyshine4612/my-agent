# sandbox/app/executor.py
# 沙箱执行器：把脚本源码落成文件、用子进程跑、收 stdout/stderr 与产物。
# 只依赖标准库 —— 镜像里每多一个包就多一分可被脚本利用的面，而收产物用不着第三方库。
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

# stdout 会进模型上下文（§5：原样回填、不截断），产物要经 HTTP 回传进后端内存，两者都必须有界
STDOUT_LIMIT = 50 * 1024
ARTIFACT_FILE_LIMIT = 20 * 1024 * 1024
ARTIFACT_TOTAL_LIMIT = 40 * 1024 * 1024
MAX_TIMEOUT = 120

# 给子进程的最小环境：不继承容器环境变量。「一个 env 都不注入」在镜像层已经做到，
# 这里是它的延伸 —— 容器内进程也不该把环境变量传给脚本。
_CHILD_ENV = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
              "PYTHONDONTWRITEBYTECODE": "1",
              "PYTHONIOENCODING": "utf-8"}


def _new_root(work_root) -> Path:
    """每次执行独占一个 uuid 目录，用完即删。

    容器是长驻的，请求在 FastAPI 线程池里真并行 —— 共用一份固定路径时，后一个请求
    会把前一个正在跑的脚本与输入文件删掉，静默给出串味的结果。
    """
    root = Path(work_root) / uuid.uuid4().hex
    for name in ("run", "work", "out"):
        (root / name).mkdir(parents=True)
    return root


def _collect_artifacts(out_dir: Path) -> tuple[list[dict], str]:
    """收集产物为 [{name, data(base64)}]，返回 (产物, 错误说明)。超限即截停并带错返回。"""
    artifacts, total, err = [], 0, ""
    for f in sorted(out_dir.rglob("*")):
        if not f.is_file():
            continue
        size = f.stat().st_size
        if size > ARTIFACT_FILE_LIMIT:
            err = f"产物 {f.name} 超过单文件上限"
            break
        if total + size > ARTIFACT_TOTAL_LIMIT:
            err = "产物总大小超过上限，已截断"
            break
        artifacts.append({"name": f.name, "data": base64.b64encode(f.read_bytes()).decode()})
        total += size
    return artifacts, err


def run_script(script: str, args: dict, files: list[dict], timeout: int, work_root) -> dict:
    """执行一段脚本源码，返回 {exit_code, stdout, stderr, artifacts, duration_ms}。

    脚本契约见设计 §5：从 stdin 读 {"args", "work_dir", "out_dir"}；
    /tmp/out 下的文件被收集为产物；stdout 是模型能看见的全部。
    """
    started = time.monotonic()
    timeout = max(1, min(int(timeout), MAX_TIMEOUT))
    root = _new_root(work_root)
    run_dir, work_dir, out_dir = root / "run", root / "work", root / "out"

    try:
        script_path = run_dir / "script.py"
        script_path.write_text(script, encoding="utf-8")
        for f in files:
            # 只取末段：name 由后端填（file_id 是 uuid），但沙箱不该假设上游一定可信
            (work_dir / Path(f["name"]).name).write_bytes(base64.b64decode(f["data"]))

        payload = json.dumps({"args": args, "work_dir": str(work_dir), "out_dir": str(out_dir)})
        try:
            proc = subprocess.run([sys.executable, str(script_path)], cwd=str(run_dir), input=payload,
                                  capture_output=True, text=True, timeout=timeout, env=_CHILD_ENV)
            exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
        except subprocess.TimeoutExpired as e:
            exit_code = 124
            stdout = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            stderr = f"执行超时（>{timeout}s）"

        if len(stdout) > STDOUT_LIMIT:
            stdout = stdout[:STDOUT_LIMIT] + "\n[stdout 超限，已截断]"

        artifacts, art_err = _collect_artifacts(out_dir)
        if art_err:
            stderr = f"{stderr}\n{art_err}".strip()

        return {"exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                "artifacts": artifacts, "duration_ms": int((time.monotonic() - started) * 1000)}
    finally:
        # 产物已 base64 进内存，目录可以立刻删：长驻容器里不该攒下上一次的残渣
        shutil.rmtree(root, ignore_errors=True)
