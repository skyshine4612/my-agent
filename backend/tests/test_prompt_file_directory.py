# 文件清单渲染测试：文案集中在 prompts.py，source 标注错了模型就分不清哪个文件是自己刚生成的。
from app.core.prompts import render_file_directory, render_system_prompt, render_user_message


def test_renders_one_line_per_file_with_size_and_source():
    """一行一个文件：名字 + 体积 + 来源；不带 file_id——模型按文件名引用，uuid 是它不该看见的内部键。

    体积按 KB/MB 分档：5788 字节渲染成 0.0MB，模型对「哪个文件更大」就失去判断。
    """
    out = render_file_directory({
        "销售表.xlsx": {"file_id": "abc", "name": "销售表.xlsx", "size": 2411724,
                        "source": "upload"},
        "已脱敏.xlsx": {"file_id": "def", "name": "已脱敏.xlsx", "size": 5788,
                        "source": "script:excel/desensitize"},
    })
    assert out == "- 销售表.xlsx  2.3MB\n- 已脱敏.xlsx  5.7KB（由 excel/desensitize 生成）"


def test_empty_mapping_renders_empty_string():
    assert render_file_directory({}) == ""


def test_system_prompt_has_no_file_blocks():
    """两份清单都不进 system prompt：{turn_files} 下一轮就读不到，{file_directory} 是每轮重发的重复源。

    留着还会与 user 正文里的折叠块、list_uploaded_files 的返回值构成同一件事的第三种来源，
    对不上时模型会照着一份已过时的清单去点文件名。
    """
    out = render_system_prompt("- travel：x", "规则", today="t", long_term_hint="")
    for slot in ("{turn_files}", "{file_directory}", "本轮用户上传的文件", "【本会话可用文件】"):
        assert slot not in out
    # 清单改由工具按需查，system 里留下的是这条指引
    assert "list_uploaded_files" in out


def test_render_user_message_folds_attachments_into_text():
    """附件折进 user 正文：system 的本轮块只覆盖本轮，正文才会随短期记忆留到下一轮。"""
    assert render_user_message("帮我把excel文件做下脱敏",
                               [{"file_id": "abc", "name": "大文件.xlsx", "size": 5748559}]) == (
        "帮我把excel文件做下脱敏\n[本条消息携带的文件]\n- 大文件.xlsx  5.5MB")
    # 没附件时原样：历史里已存的旧消息是纯文本，两种都要喂得进模型
    assert render_user_message("成都三日游", []) == "成都三日游"
