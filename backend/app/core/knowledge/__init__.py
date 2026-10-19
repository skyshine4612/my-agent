# app/core/knowledge/__init__.py
# 知识库流水线：切块 / 向量化 / 抓取入库 / 检索 / 图片描述。
# 对外保持原 app.core.knowledge 的公开符号，内部实现拆在子模块。
from app.core.knowledge.store import SIMILARITY_FLOOR, KnowledgeStore, filter_relevant

__all__ = ["SIMILARITY_FLOOR", "KnowledgeStore", "filter_relevant"]
