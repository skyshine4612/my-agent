# 文件清单渲染测试：文案集中在 prompts.py，source 标注错了模型就分不清哪个文件是自己刚生成的。
from app.core.prompts import (render_file_directory, render_system_prompt, render_turn_files,
                              render_user_message)


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


def test_same_name_keeps_only_newest():
    """同名多条只留最新的一条。

    同名 = 同一个文件的重复投递（用户没看到反馈就会重传，实测传了 4 次），
    清单里堆 4 条同名会让模型反问「你要处理哪个」而不动手。
    调用方按 created_at 倒序传入，所以第一条就是最新那次。
    """
    out = render_file_directory([
        {"file_id": "new", "name": "测试数据.xlsx", "size": 5788, "source": "upload"},
        {"file_id": "old", "name": "测试数据.xlsx", "size": 5788, "source": "upload"},
    ])
    assert "file_id: new" in out
    assert "old" not in out
    assert out.count("测试数据.xlsx") == 1


def test_small_file_size_in_kb():
    """小于 1MB 的按 KB 显示：5788 字节渲染成 0.0MB，模型对「哪个文件更大」失去判断。"""
    out = render_file_directory([{"file_id": "a", "name": "小表.xlsx", "size": 5788,
                                  "source": "upload"}])
    assert "5.7KB" in out


def test_system_prompt_gets_file_slot():
    out = render_system_prompt("- travel：x", "规则", today="今天是 2026-09-12。",
                               long_term_hint="", file_directory="- file_id: abc  名称: a.xlsx")
    assert "- file_id: abc" in out
    assert "{file_directory}" not in out


def test_system_prompt_renders_none_when_no_files():
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    assert "【本会话可用文件】\n无" in out


def test_renders_turn_files_block():
    """本轮上传件单独成块：这次要处理谁必须是事实，不能让模型从整会话清单里猜。"""
    out = render_turn_files([{"file_id": "abc", "name": "大文件.xlsx", "size": 5748559}])
    assert out == "- file_id: abc  名称: 大文件.xlsx  5.5MB"


def test_turn_files_reach_system_prompt():
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="",
                               turn_files=render_turn_files(
                                   [{"file_id": "abc", "name": "a.xlsx", "size": 1}]))
    assert "【本轮用户上传的文件】\n- file_id: abc" in out
    assert "{turn_files}" not in out


def test_turn_files_empty_renders_none():
    """没上传件的轮次该栏显示「无」——system.md 里的规则要靠它判断该去哪一栏找文件。"""
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    assert "【本轮用户上传的文件】\n无" in out


def test_render_user_message_folds_attachments_into_text():
    """附件折进 user 正文：system 的本轮块只覆盖本轮，正文才会随短期记忆留到下一轮。"""
    out = render_user_message("帮我把excel文件做下脱敏",
                              [{"file_id": "abc", "name": "大文件.xlsx", "size": 5748559}])
    assert out == ("帮我把excel文件做下脱敏\n[本条消息携带的文件]\n"
                   "- file_id: abc  名称: 大文件.xlsx  5.5MB")


def test_render_user_message_without_files_is_verbatim():
    """没附件时正文原样——历史里已存的旧消息也是纯文本，两种都要喂得进模型。"""
    assert render_user_message("成都三日游", []) == "成都三日游"
