# tests/test_trip_service.py
# AgentService 装配契约测试：验证所有子 Agent 都被注入了 llm（否则 agent.run 时 llm=None 会崩）。
from app.config import settings
from app.services.trip_service import AgentService


def test_service_injects_llm_into_all_agents(tmp_path, monkeypatch):
    """初始化 AgentService（无真实 key 时用 FallbackLLM，可正常初始化），
    断言注册表里每个子 Agent 的 llm 均不为 None——这是关键装配校验。"""
    # 用临时 SQLite，避免污染仓库默认 app.db
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "svc.db"))
    svc = AgentService()
    names = svc.agents.list_names()
    assert names, "Agent 注册表不应为空"
    for name in names:
        assert svc.agents.get(name).llm is not None, f"子 Agent {name} 未注入 llm"
