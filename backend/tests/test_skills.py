# tests/test_skills.py
# skill 加载器契约测试：load_skills 扫描 SKILL.md 提取 name/description/body、get_skill_body 按名取正文。
from app.core.skills import load_skills, get_skill_body


def test_load_skills_scans_travel():
    """load_skills 扫描 skills/ 目录，返回 travel skill 的 name/description/body。"""
    skills = load_skills()
    names = [s["name"] for s in skills]
    assert "travel" in names
    travel = next(s for s in skills if s["name"] == "travel")
    assert travel["description"] == "旅行规划（行程/交通/景点/天气/美食/预算）"
    assert "train_ticket_query" in travel["body"]


def test_get_skill_body_returns_body():
    """get_skill_body 按名返回正文；未知名返回空串。"""
    body = get_skill_body("travel")
    assert "train_ticket_query" in body
    assert get_skill_body("unknown") == ""
