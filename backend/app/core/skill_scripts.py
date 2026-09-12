# app/core/skill_scripts.py
# 脚本白名单：扫描 app/skills/*/scripts/*.py，建立 {(skill, script): Path} 索引。
# 这是整条沙箱链路的安全边界——模型只能从这份清单里挑脚本，代码本体由后端从磁盘读取、不经模型的手。
# 目录约定与 skills/ 一致：一个业务一个目录，脚本放它下面的 scripts/。
from pathlib import Path

# 本文件在 app/core/，往上两级即 app/，与 skills.py 的 SKILLS_DIR 指向同一处
SKILLS_DIR = Path(__file__).parent.parent / "skills"


def scan_scripts(skills_dir=None) -> dict:
    """扫描 skills/<skill>/scripts/<script>.py，返回 {(skill, script): Path}。

    下划线开头的文件不计入：它们是脚本的内部模块（被脚本 import），不是可被模型选中的入口。
    没有 scripts/ 目录的业务直接跳过——本期只有 excel 一个业务带脚本。
    """
    root = Path(skills_dir) if skills_dir is not None else SKILLS_DIR
    index = {}
    if not root.exists():
        return index
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        scripts_dir = skill_dir / "scripts"
        if not scripts_dir.is_dir():
            continue
        for f in sorted(scripts_dir.glob("*.py")):
            if f.name.startswith("_"):
                continue
            index[(skill_dir.name, f.stem)] = f
    return index
