# tests/test_memory_metrics.py
# 记忆体系的计数器：证明各机制确实被触发过，而非纸面设计。
from app.core.memory.metrics import MEMORY_METRICS, bump, snapshot


def test_bump_increments():
    before = snapshot().get("working_evicted_units", 0)
    bump("working_evicted_units")
    assert snapshot()["working_evicted_units"] == before + 1


def test_snapshot_is_a_copy():
    """snapshot 返回副本，外部改动不影响内部状态。"""
    s = snapshot()
    s["working_evicted_units"] = 9999
    assert snapshot().get("working_evicted_units", 0) != 9999
