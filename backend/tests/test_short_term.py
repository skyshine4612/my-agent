# tests/test_short_term.py
# 短期记忆契约测试：写入/UPSERT 去重/按 conversation_id 查询与隔离/清理
import pytest

from app.core.memory import ShortTermMemory


@pytest.mark.asyncio
async def test_write_and_get_all(tmp_path):
    """写入后按 conversation_id 取回，且不同会话隔离。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.write("conv1", "weather_query", {"city": "泰安"}, "晴33℃")
    await s.write("conv1", "poi_search", {"city": "泰安"}, "泰山/岱庙")
    rows = await s.get_all("conv1")
    assert [r["tool_name"] for r in rows] == ["weather_query", "poi_search"]
    assert await s.get_all("conv2") == []  # 会话隔离


@pytest.mark.asyncio
async def test_upsert_dedup(tmp_path):
    """同 (conversation_id, tool_name, args) 覆盖旧行而非新增（去重核心）。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.write("conv1", "weather_query", {"city": "泰安", "days": 3}, "晴33℃")
    await s.write("conv1", "weather_query", {"city": "泰安", "days": 3}, "雨26℃")
    rows = await s.get_all("conv1")
    assert len(rows) == 1  # 覆盖而非新增
    assert rows[0]["summary"] == "雨26℃"  # summary 更新为最新


@pytest.mark.asyncio
async def test_upsert_distinct_args_kept(tmp_path):
    """不同 args 是不同记录，不去重。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.write("conv1", "weather_query", {"city": "泰安"}, "晴")
    await s.write("conv1", "weather_query", {"city": "济南"}, "雨")
    assert len(await s.get_all("conv1")) == 2


@pytest.mark.asyncio
async def test_clear_conversation(tmp_path):
    """清理单会话的短期记忆，不影响其他会话。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.write("conv1", "weather_query", {"city": "泰安"}, "晴")
    await s.write("conv2", "poi_search", {"city": "济南"}, "趵突泉")
    await s.clear_conversation("conv1")
    assert await s.get_all("conv1") == []
    assert len(await s.get_all("conv2")) == 1  # 其他会话不受影响
