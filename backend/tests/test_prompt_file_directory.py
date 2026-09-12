# 文件清单渲染测试：文案集中在 prompts.py，source 标注错了模型就分不清哪个文件是自己刚生成的。
from app.core.prompts import (render_file_directory, render_system_prompt, render_turn_files,
                              render_user_message)


def test_renders_upload_line():
    out = render_file_directory({"销售表.xlsx": {"file_id": "abc", "name": "销售表.xlsx",
                                                "size": 2411724, "source": "upload"}})
    assert "销售表.xlsx" in out and "2.3MB" in out
    assert "生成" not in out


def test_marks_script_artifacts_with_source():
    out = render_file_directory({"已脱敏.xlsx": {"file_id": "def", "name": "已脱敏.xlsx",
                                                "size": 1024, "source": "script:excel/desensitize"}})
    assert "由 excel/desensitize 生成" in out


def test_empty_mapping_renders_empty_string():
    assert render_file_directory({}) == ""


def test_no_file_id_in_output():
    """清单里不再出现 file_id：模型按文件名引用，uuid 是它不该看见的内部键。"""
    out = render_file_directory({"a.xlsx": {"file_id": "abc", "name": "a.xlsx", "size": 1024,
                                            "source": "upload"}})
    assert out == "- a.xlsx  1.0KB"


def test_small_file_size_in_kb():
    """小于 1MB 的按 KB 显示：5788 字节渲染成 0.0MB，模型对「哪个文件更大」失去判断。"""
    out = render_file_directory({"小表.xlsx": {"file_id": "a", "name": "小表.xlsx", "size": 5788,
                                              "source": "upload"}})
    assert "5.7KB" in out


def test_system_prompt_has_no_file_directory_slot():
    """会话文件清单已改为按需查，system.md 里不该再留这一栏。

    留着就是每轮重发一份随时在变的清单，且与 list_uploaded_files 是同一件事的两种来源，
    两处渲染对不上时模型会照着一份已过时的清单去点文件名。
    """
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    assert "{file_directory}" not in out
    assert "【本会话可用文件】" not in out
    # 空清单的兜底文案也不再出现：那是清单块的尾巴，块没了它就没意义了
    assert "list_uploaded_files" in out


def test_renders_turn_files_block():
    """本条消息携带的件单独成块：这次要处理谁必须是事实，不能让模型从整会话清单里猜。"""
    out = render_turn_files([{"file_id": "abc", "name": "大文件.xlsx", "size": 5748559}])
    assert out == "- 大文件.xlsx  5.5MB"


def test_system_prompt_has_no_turn_files_slot():
    """本轮携带的件已折进 user 正文，system.md 里不该再留这一栏。

    留着就是每轮重发一份只对本轮有效的清单，下一轮就没了；而且它与正文里的折叠块
    本就是同一件事的两种写法，模型看到两份还可能对不上。
    """
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    assert "{turn_files}" not in out
    assert "本轮用户上传的文件" not in out


def test_render_user_message_folds_attachments_into_text():
    """附件折进 user 正文：system 的本轮块只覆盖本轮，正文才会随短期记忆留到下一轮。"""
    out = render_user_message("帮我把excel文件做下脱敏",
                              [{"file_id": "abc", "name": "大文件.xlsx", "size": 5748559}])
    assert out == "帮我把excel文件做下脱敏\n[本条消息携带的文件]\n- 大文件.xlsx  5.5MB"


def test_render_user_message_without_files_is_verbatim():
    """没附件时正文原样——历史里已存的旧消息也是纯文本，两种都要喂得进模型。"""
    assert render_user_message("成都三日游", []) == "成都三日游"
