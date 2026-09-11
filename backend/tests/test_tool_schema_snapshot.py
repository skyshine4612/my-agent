# tests/test_tool_schema_snapshot.py
# 工具 schema 金标准：本次迁移把工具从手写 JSON Schema 换成 pydantic 生成，
# 参数名/类型/描述/必填性必须逐字段不变，故落锁；任何漂移立即转红。
import json
import pathlib

import pytest

from app.services.agent_service import service

GOLDEN = pathlib.Path(__file__).parent / "data" / "tool_schemas.json"


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(_golden()))
def test_tool_schema_matches_golden(name):
    """逐个工具比对：参数名、类型、description、required 必须与快照完全一致。"""
    live = service.registry.to_openai_schemas([name])
    assert len(live) == 1, f"{name} 未注册"
    assert live[0] == _golden()[name]


def test_golden_covers_every_registered_tool():
    """快照不得漏工具：新增/删除工具要同步更新快照，否则这条转红。"""
    assert sorted(_golden()) == sorted(service.registry.list_names())
