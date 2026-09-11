# app/core/memory/metrics.py
# 记忆机制计数器：记录各机制被触发的次数，供重构前后对比与线上观察。
import threading

_LOCK = threading.Lock()
_METRICS: dict[str, int] = {}

# 指标含义：
#   working_evicted_units    工作记忆成对淘汰的单元数
#   working_distill_calls    蒸馏 LLM 调用次数（每次淘汰一批即一次）
#   working_distill_fallbacks 蒸馏失败降级为拼接截断的次数
#   spill_writes            工具结果落盘次数
#   dedup_hits              长期记忆语义去重命中次数（未新增、只提升 importance）
_METRIC_NAMES = (
    "working_evicted_units",
    "working_distill_calls",
    "working_distill_fallbacks",
    "spill_writes",
    "dedup_hits",
)

# 同一份指标名单的公开别名（测试与外部观察者按此断言全集）；只暴露名单，不暴露内部计数字典
MEMORY_METRICS = _METRIC_NAMES


def bump(name: str, n: int = 1) -> None:
    """计数 +n。线程安全。"""
    with _LOCK:
        _METRICS[name] = _METRICS.get(name, 0) + n


def snapshot() -> dict[str, int]:
    """返回当前计数的副本。"""
    with _LOCK:
        return dict(_METRICS)


def log_snapshot() -> None:
    """把当前计数打一条 INFO 日志（未触发过的指标显示 0）。"""
    import logging
    snap = snapshot()
    logging.getLogger(__name__).info(
        "[记忆指标] %s", " ".join(f"{k}={snap.get(k, 0)}" for k in _METRIC_NAMES))
