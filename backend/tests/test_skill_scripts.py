# 脚本白名单扫描测试：这是整条沙箱链路的边界，错了等于边界漏了，必须由测试钉住。
from app.core.skill_scripts import scan_scripts


def _make(tmp_path, skill, scripts):
    d = tmp_path / skill / "scripts"
    d.mkdir(parents=True)
    for name in scripts:
        (d / name).write_text("print(1)", encoding="utf-8")
    return tmp_path


def test_finds_script_entries(tmp_path):
    root = _make(tmp_path, "excel", ["inspect.py", "desensitize.py"])
    index = scan_scripts(root)
    assert set(index) == {("excel", "inspect"), ("excel", "desensitize")}
    assert index[("excel", "inspect")].name == "inspect.py"


def test_skips_underscore_modules(tmp_path):
    # 下划线开头是脚本的内部模块（被脚本 import），不是可被模型选中的入口
    root = _make(tmp_path, "excel", ["desensitize.py", "_rules.py"])
    assert set(scan_scripts(root)) == {("excel", "desensitize")}


def test_skill_without_scripts_dir_is_skipped(tmp_path):
    (tmp_path / "travel").mkdir()
    (tmp_path / "travel" / "SKILL.md").write_text("---\nname: travel\n---\n", encoding="utf-8")
    assert scan_scripts(tmp_path) == {}


def test_missing_root_returns_empty(tmp_path):
    assert scan_scripts(tmp_path / "nope") == {}
