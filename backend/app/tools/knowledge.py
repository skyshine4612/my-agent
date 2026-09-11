# app/tools/knowledge.py
# 知识库工具：search_knowledge，语义检索已入库的攻略语料。
# user_id 走 contextvar，与 read_file/grep 同路子——工具入参只由 LLM 决定，身份不能由它给。
from app.core.context import current_user_id

# search_knowledge 工具的 OpenAI function-calling schema
SEARCH_KNOWLEDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_knowledge",
        "description": "在本地知识库中按语义检索攻略/游记语料，返回最相关的片段。"
                       "当需要目的地的深度玩法、避坑经验、行程细节等攻略类信息时使用。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题，用自然语言，如「杭州三天怎么玩」"},
                "top_k": {"type": "integer", "description": "返回片段数，默认 3"},
            },
            "required": ["query"],
        },
    },
}


def make_search_knowledge(store):
    """构造 search_knowledge 工具：闭包 KnowledgeStore，按当前用户身份检索。"""

    async def search_knowledge(query, top_k=3):
        """语义检索知识库，返回最相关片段（含标题与出处）；空库时如实说明。"""
        hits = await store.search(current_user_id.get(), query, top_k)
        if not hits:
            return "知识库暂无相关内容"
        return hits

    return search_knowledge


def register_knowledge_tools(registry, store):
    """把 search_knowledge 注入 ToolRegistry（需要 store，故在 service 层装配时注册）。"""
    fn = make_search_knowledge(store)
    fn.__name__ = "search_knowledge"
    fn.description = SEARCH_KNOWLEDGE_TOOL["function"]["description"]
    fn.parameters = SEARCH_KNOWLEDGE_TOOL["function"]["parameters"]
    registry.register("search_knowledge", fn.description, fn.parameters, fn, "检索知识库")
