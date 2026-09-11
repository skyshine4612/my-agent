# app/tools/knowledge.py
# 知识库工具：search_knowledge，语义检索已入库的攻略语料。
# user_id 走 contextvar，与 read_file/grep 同路子——工具入参只由 LLM 决定，身份不能由它给。
# 工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_user_id


def make_search_knowledge(store):
    """构造 search_knowledge 工具：闭包 KnowledgeStore，按当前用户身份检索。"""

    @tool
    async def search_knowledge(
        query: Annotated[str, Field(description="检索问题，用自然语言，如「杭州三天怎么玩」")],
        top_k: Annotated[int, Field(description="返回片段数，默认 3")] = 3,
    ) -> list | str:
        """在本地知识库中按语义检索攻略/游记语料，返回最相关的片段。当需要目的地的深度玩法、避坑经验、行程细节等攻略类信息时使用。"""
        hits = await store.search(current_user_id.get(), query, top_k)
        if not hits:
            return "知识库暂无相关内容"
        return hits

    return search_knowledge


def register_knowledge_tools(registry, store):
    """把 search_knowledge 注入 ToolRegistry（需要 store，故在 service 层装配时注册）。"""
    registry.register_tool(make_search_knowledge(store), "检索知识库")
