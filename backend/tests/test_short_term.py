# tests/test_short_term.py
# 短期记忆（summary + 最近几轮对话）契约测试：replace_records / get_records / clear_conversation
import pytest

from app.core.memory import ShortTermMemory


@pytest.mark.asyncio
async def test_replace_and_get_records(tmp_path):
    """replace_records 后 get_records 按序取回（summary 在前，对话在后）。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    records = [
        {"role": "summary", "content": "早期对话摘要"},
        {"role": "user", "content": "最近一轮用户"},
        {"role": "assistant", "content": "最近一轮助手"},
    ]
    await s.replace_records("conv1", records)
    got = await s.get_records("conv1")
    assert [r["role"] for r in got] == ["summary", "user", "assistant"]
    assert got[0]["content"] == "早期对话摘要"
    assert got[1]["content"] == "最近一轮用户"


@pytest.mark.asyncio
async def test_replace_records_overwrites(tmp_path):
    """同会话再次 replace_records 清空旧记录、只保留新记录。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.replace_records("conv1", [{"role": "user", "content": "旧"}])
    await s.replace_records("conv1", [{"role": "summary", "content": "新摘要"}])
    got = await s.get_records("conv1")
    assert len(got) == 1
    assert got[0]["content"] == "新摘要"


@pytest.mark.asyncio
async def test_records_isolation(tmp_path):
    """不同会话的短期记忆互相隔离。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.replace_records("conv1", [{"role": "summary", "content": "A"}])
    await s.replace_records("conv2", [{"role": "summary", "content": "B"}])
    assert (await s.get_records("conv1"))[0]["content"] == "A"
    assert (await s.get_records("conv2"))[0]["content"] == "B"


@pytest.mark.asyncio
async def test_clear_conversation(tmp_path):
    """clear_conversation 删除单会话，不影响其他会话。"""
    s = ShortTermMemory(str(tmp_path / "st.db"))
    await s.replace_records("conv1", [{"role": "summary", "content": "A"}])
    await s.replace_records("conv2", [{"role": "summary", "content": "B"}])
    await s.clear_conversation("conv1")
    assert await s.get_records("conv1") == []
    assert len(await s.get_records("conv2")) == 1
