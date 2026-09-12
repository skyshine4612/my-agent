# 文件清单渲染测试：文案集中在 prompts.py，source 标注错了模型就分不清哪个文件是自己刚生成的。
from app.core.prompts import render_file_directory, render_system_prompt


def test_renders_upload_line():
    out = render_file_directory([{"file_id": "abc", "name": "销售表.xlsx", "size": 2411724,
                                  "source": "upload"}])
    assert "file_id: abc" in out and "销售表.xlsx" in out and "2.3MB" in out
    assert "生成" not in out


def test_marks_script_artifacts_with_source():
    out = render_file_directory([{"file_id": "def", "name": "已脱敏.xlsx", "size": 1024,
                                  "source": "script:excel/desensitize"}])
    assert "由 excel/desensitize 生成" in out


def test_empty_list_renders_empty_string():
    assert render_file_directory([]) == ""


def test_system_prompt_gets_file_slot():
    out = render_system_prompt("- travel：x", "规则", today="今天是 2026-09-12。",
                               long_term_hint="", file_directory="- file_id: abc  名称: a.xlsx")
    assert "- file_id: abc" in out
    assert "{file_directory}" not in out


def test_system_prompt_renders_none_when_no_files():
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    assert "【本会话可用文件】\n无" in out
