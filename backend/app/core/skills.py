# app/core/skills.py
# Skill 加载器：扫描 skills/ 目录，解析 SKILL.md 的 YAML frontmatter。
# 标准 skill 两段式：清单（name + description）常驻 system prompt，正文由 get_skill 工具按需加载。
import yaml
from pathlib import Path

# skills 目录位于 app/skills/：本文件在 app/core/，往上两级即 app/
SKILLS_DIR = Path(__file__).parent.parent / "skills"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 SKILL.md 开头的 YAML frontmatter，返回 (metadata, body)。

    frontmatter 以 --- 起止（第一行与收尾行都必须恰为 ---）；正文为收尾 --- 之后的内容（strip 首尾空白）。
    无 frontmatter / YAML 解析失败 / 结构非 dict 时，按空元数据 + 全文作正文兜底。
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}, text.strip()

    # 从第二行起找第一个恰为 --- 的行作为收尾行（容忍 CRLF，用 rstrip 去掉行尾换行再比较）
    closing_index = next(
        (i for i, line in enumerate(lines[1:], start=1)
         if line.rstrip("\r\n") == "---"),
        None,
    )
    if closing_index is None:
        return {}, text.strip()

    frontmatter = "".join(lines[1:closing_index])
    body = "".join(lines[closing_index + 1:]).strip()
    # YAML 解析失败或结果非 dict（如 top-level 是 list/标量）都按空元数据兜底
    try:
        metadata = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    return metadata, body


def _load_skill(name: str) -> dict[str, str] | None:
    """读取 skills/<name>/SKILL.md，返回 {name, description, body}；文件缺失返回 None。"""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    metadata, body = _parse_frontmatter(text)

    # YAML 会把 yes/no/数字/日期 等裸标量解析成 bool/int/date，这里做类型归一化：
    # 只有字符串才 strip，非字符串（或缺失）按目录名/空串兜底。
    raw_name = metadata.get("name")
    resolved_name = raw_name.strip() if isinstance(raw_name, str) else name
    raw_description = metadata.get("description")
    description = raw_description.strip() if isinstance(raw_description, str) else ""

    return {
        "name": resolved_name,
        "description": description,
        "body": body,
    }


def load_skills() -> list[dict[str, str]]:
    """扫描 skills/ 下所有 SKILL.md，返回 [{name, description, body}]（按目录名排序，确定性）。"""
    if not SKILLS_DIR.is_dir():
        return []
    skills: list[dict[str, str]] = []
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        s = _load_skill(d.name)
        if s is not None:
            skills.append(s)
    return skills


def get_skill_body(name: str) -> str:
    """按 skill 名返回正文；未命中返回空串。"""
    s = _load_skill(name)
    return s["body"] if s else ""