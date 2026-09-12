# tool_result 事件的构造：脚本产物清单要单独带一份结构化字段。
# result 是给人看的文本（还可能被截断），下载要的是 file_id 与文件名，从文本里再解析出来不可靠。
from app.core.agent import _tool_result_event


def test_plain_result_has_no_artifacts():
    ev = _tool_result_event("main", "weather_query", "查天气", "tc1", "晴", {"temp": 30})
    assert ev["type"] == "tool_result"
    assert ev["result"] == "晴" and ev["id"] == "tc1"
    assert "artifacts" not in ev


def test_script_artifacts_pass_through():
    arts = [{"file_id": "a1", "name": "已脱敏.xlsx", "size": 2048}]
    ev = _tool_result_event("main", "run_skill_script", "执行数据处理脚本", "tc2", "已处理 1 行",
                            {"ok": True, "output": "已处理 1 行", "artifacts": arts})
    assert ev["artifacts"] == arts


def test_empty_artifacts_not_attached():
    ev = _tool_result_event("main", "run_skill_script", "执行数据处理脚本", "tc3", "", {"ok": True, "artifacts": []})
    assert "artifacts" not in ev


def test_tool_result_event_carries_images():
    """图片与 artifacts 并列走侧信道，不塞进 artifacts —— 后者的语义是「可下载的会话文件」。"""
    ev = _tool_result_event("assistant", "search_knowledge", "检索知识库", "id1", "文本",
                            {"hits": [], "images": [{"key": "a/b/0.jpg", "desc": "西湖"}]})
    assert ev["images"] == [{"key": "a/b/0.jpg", "desc": "西湖"}]


def test_tool_result_event_without_images_has_no_key():
    ev = _tool_result_event("assistant", "read_file", "读取结果文件", "id1", "文本", "raw")
    assert "images" not in ev
