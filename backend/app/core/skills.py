# app/core/skills.py
# Skill 加载器：扫描 skills/ 目录，解析 SKILL.md 的 YAML frontmatter。
# 标准 skill 两段式：清单（name + description）常驻 system prompt，正文由 get_skill 工具按需加载。
import re
from pathlib import Path

# skills 目录位于 app/skills/：本文件在 app/core/，往上两级即 app/
SKILLS_DIR = Path(__file__).parent.parent / "skills"

# 匹配开头的 YAML frontmatter（--- ... ---）
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 开头的 YAML frontmatter，提取 name / description（缺失返回空串）。"""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in ("name", "description"):
                meta[key] = val
    return meta


def _load_skill(name: str) -> dict[str, str] | None:
    """读取 skills/<name>/SKILL.md，返回 {name, description, body}；文件缺失返回 None。"""
    path = SKILLS_DIR / name / "SKILL.md"
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    meta = _parse_frontmatter(text)
    m = _FRONTMATTER_RE.match(text)
    body = text[m.end():].strip() if m else text.strip()
    return {
        "name": meta.get("name", name),
        "description": meta.get("description", ""),
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
