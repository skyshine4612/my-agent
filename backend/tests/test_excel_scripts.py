# Excel skill 脚本单测：只测纯函数，不碰 openpyxl
# （脚本把 openpyxl 延迟到 main() 里 import，宿主不装它也能跑这些用例）。
import importlib.util
import os
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "app" / "skills" / "excel" / "scripts"

HEADER = ["姓名", "联系电话", "邮箱", "身份证", "银行卡", "金额"]
ROWS = [["张三", "13812341234", "zhangsan@qq.com", "110101199001011234", "6222021234567890", 100]]


def load(name):
    """按路径加载脚本模块：脚本不是包（没法 import），只能从文件加载。"""
    spec = importlib.util.spec_from_file_location(f"skill_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mask(column, rule, header=HEADER, rows=ROWS, salt=""):
    """跑一次 apply_rules 取第一行结果（省得每条用例都解包三元组）。"""
    _, new_rows, _ = load("desensitize").apply_rules(header, rows, [{"column": column, "rule": rule}], salt)
    return new_rows[0]


def test_phone_middle4():
    assert mask("联系电话", "phone_middle4")[1] == "138****1234"


def test_name_keep_first():
    assert mask("姓名", "name_keep_first")[0] == "张*"


def test_idcard_keep_ends():
    assert mask("身份证", "idcard_keep_ends")[3] == "110101********1234"


def test_email_mask():
    assert mask("邮箱", "email_mask")[2] == "z***@qq.com"


def test_bankcard_keep_last4():
    assert mask("银行卡", "bankcard_keep_last4")[4] == "************7890"


def test_hash_is_stable_and_salted():
    d = load("desensitize")
    op = [{"column": "姓名", "rule": "hash"}]
    once = d.apply_rules(HEADER, ROWS, op, "s1")[1][0][0]
    twice = d.apply_rules(HEADER, ROWS, op, "s1")[1][0][0]
    other = d.apply_rules(HEADER, ROWS, op, "s2")[1][0][0]
    # 同值同结果（保住可关联性），换 salt 换结果，长度固定 16
    assert once == twice and once != other and len(once) == 16


def test_drop_removes_column():
    header, rows, keep = load("desensitize").apply_rules(HEADER, ROWS, [{"column": "金额", "rule": "drop"}])
    assert "金额" not in header
    assert len(header) == 5 and len(rows[0]) == 5
    assert keep == [0, 1, 2, 3, 4]


def test_untouched_column_keeps_value_and_type():
    # 金额没被规则碰过，要原样是数字：转成字符串会毁掉 Excel 里的数值格式
    assert mask("姓名", "name_keep_first")[5] == 100


def test_value_not_matching_rule_is_kept():
    rows = [["张三", "无", "x", "y", "z", 1]]
    out = load("desensitize").apply_rules(HEADER, rows, [{"column": "联系电话", "rule": "phone_middle4"}])
    assert out[1][0][1] == "无"


def test_unknown_column_error():
    with pytest.raises(ValueError, match="表里没有这一列"):
        mask("手机号", "phone_middle4")


def test_unknown_rule_error():
    with pytest.raises(ValueError, match="不认识的规则"):
        mask("联系电话", "不存在的规则")


def test_empty_ops_rejected():
    with pytest.raises(ValueError, match="ops 不能为空"):
        load("desensitize").apply_rules(HEADER, ROWS, [])


def test_resolve_does_not_truncate_long_name():
    # 长中文名越过 SAMPLE_CHARS=32：拿样本值那套截断去处理路径就会找一个不存在的文件
    name = "2026年第三季度华东区销售明细汇总表（含税、已审核版）.xlsx"
    assert len(name) > 32
    # 用 os.path.join 拼期望值：沙箱是 Linux，但这条断言跑在 Windows（分隔符不同）
    assert load("inspect")._resolve({"file": name}, "/tmp/w") == os.path.join("/tmp/w", name)


def test_resolve_keeps_lookups_inside_work_dir():
    """绝对路径与 ../ 都被 basename 压回 work_dir：名字只用于查表，不参与路径拼接。"""
    for script in ("inspect", "desensitize"):
        assert load(script)._resolve({"file": "/etc/passwd"}, "/tmp/w") == os.path.join("/tmp/w", "passwd")
        assert load(script)._resolve({"file": "../../x.xlsx"}, "/tmp/w") == os.path.join("/tmp/w", "x.xlsx")


def test_resolve_lists_available_files_when_missing(tmp_path):
    """名字对不上时把 work_dir 里实际有什么回给模型，让它一轮自纠，而不是反复填同一个错名字。"""
    (tmp_path / "测试数据.xlsx").write_bytes(b"x")
    for script in ("inspect", "desensitize"):
        with pytest.raises(SystemExit, match="本次可用：测试数据.xlsx"):
            load(script)._resolve({}, str(tmp_path))


def test_summarize_sheet_reads_columns_and_samples():
    i = load("inspect")
    rows = [["姓名", "联系电话"], ["张三", "13812341234"], ["李四", "13900001111"]]
    s = i.summarize_sheet("Sheet1", rows, 1200)
    assert s["columns"] == ["姓名", "联系电话"]
    assert s["row_count"] == 1200
    assert s["samples"]["联系电话"] == ["13812341234", "13900001111"]


def test_summarize_sheet_header_row_and_fallback_name():
    i = load("inspect")
    rows = [["报表说明", ""], ["", "金额"], ["甲", 1]]
    s = i.summarize_sheet("Sheet1", rows, 3, header_row=2)
    # 空表头列用位置兜底命名：脱敏要按列名指定，无名列也得有个能指认的名字
    assert s["columns"] == ["第1列", "金额"]
    assert s["preview"][0] == ["报表说明", ""]
