# tests/test_tool_schema_snapshot.py
# 工具 schema 金标准：参数名/类型/描述/必填性锁成快照，任何漂移立即转红。
import json
import pathlib

import pytest

from app.services.agent_service import service

GOLDEN = pathlib.Path(__file__).parent / "data" / "tool_schemas.json"


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def _project(schema: dict) -> dict:
    """把 schema 投影成要比对的契约：工具名、工具描述、每个参数的
    名称/类型/description/其余一切键（含 enum）、required 清单。

    **刻意排除 `default`**：默认值同时写在参数描述文案里（「默认 20」），
    default 字段是同一信息的第二份载体，排掉它比对才稳定。
    用黑名单而非白名单排除，是为了让 pydantic 新增的任何键都被这条测试抓住。
    默认值的正确性由行为用例（如 test_weather_query_default_days）覆盖。
    """
    fn = schema["function"]
    params = fn["parameters"]
    return {
        "name": fn["name"],
        "description": fn["description"],
        "parameters": {
            **{k: v for k, v in params.items() if k not in ("properties", "required")},
            "properties": {
                name: {k: v for k, v in spec.items() if k != "default"}
                for name, spec in params.get("properties", {}).items()
            },
            "required": sorted(params.get("required", [])),
        },
    }


@pytest.mark.parametrize("name", sorted(_golden()))
def test_tool_schema_matches_golden(name):
    """逐个工具比对：参数名、类型、description、required 必须与快照完全一致。"""
    live = service.registry.to_openai_schemas([name])
    assert len(live) == 1, f"{name} 未注册"
    assert _project(live[0]) == _golden()[name]


def test_golden_covers_every_registered_tool():
    """快照不得漏工具：新增/删除工具要同步更新快照，否则这条转红。"""
    assert sorted(_golden()) == sorted(service.registry.list_names())
