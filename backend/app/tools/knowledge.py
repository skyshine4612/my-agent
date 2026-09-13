# app/tools/knowledge.py
# 知识库工具：search_knowledge，语义检索已入库的攻略语料。
# user_id 走 contextvar，与 read_file/grep 同路子——工具入参只由 LLM 决定，身份不能由它给。
# 工具用 langchain @tool 定义（schema 由 pydantic 从函数签名生成），经 registry.register_tool 注册。
from typing import Annotated

from langchain_core.tools import tool
from pydantic import Field

from app.core.context import current_user_id


def number_hit_images(hits: list[dict]) -> list[dict]:
    """把命中片段正文里的 【图片：desc】 改写成 【图{chunk_id}-{序号}：desc】，返回图片清单。

    清单保持原有顺序与去重口径（key 优先、desc 兜底），每张能取到的图多带一个 `ref`。
    号里带 **chunk 行 id** 是为了持久：同一个 chunk 的同一张图永远是同一个号，模型即使
    抄了历史回答里的号也不会指到另一张图上。它不依赖调用次数，所以不需要跨轮状态。

    配对用**游标 + 完整字面量 find**，不能用 str.replace：同一块里两张图 desc 相同时，
    replace 会把两个 marker 都写成第一个图的号。find 按出现位置顺序配对，找不到就跳过
    —— 占位符被字符硬切切开时 images 列有条目而正文没有 marker，跳过才不会错配。

    `ref` 只发给有 key 的图：上传失败（key 为 null）的图前端渲染不出来，给了号就是
    让模型去引用一张注定显示不了的图。
    """
    out: list[dict] = []
    seen: set = set()
    for h in hits:
        content = h.get("content") or ""
        cursor = 0
        pieces: list[str] = []
        for i, img in enumerate(h.get("images") or [], start=1):
            desc = img.get("desc") or ""
            literal = f"【图片：{desc}】"
            at = content.find(literal, cursor)
            if at >= 0:
                pieces.append(content[cursor:at])
                pieces.append(f"【图{h['id']}-{i}：{desc}】")
                cursor = at + len(literal)
            entry = {"key": img.get("key"), "desc": desc}
            if at >= 0 and img.get("key"):
                entry["ref"] = f"{h['id']}-{i}"
            dedup = img.get("key") or desc
            if dedup not in seen:
                seen.add(dedup)
                out.append(entry)
        if pieces:
            h["content"] = "".join(pieces + [content[cursor:]])
    return out


def make_search_knowledge(store):
    """构造 search_knowledge 工具：闭包 KnowledgeStore，按当前用户身份检索。

    **不进 NON_QUERY_TOOLS**：进了就既不截断也不 spill，整段灌进上下文。返回值里带图
    不代表怕截断 —— 图片走 `_tool_result_event` 的侧信道，读的是 raw 而非截断后的 text。
    """

    @tool
    async def search_knowledge(
        query: Annotated[str, Field(description="检索问题，用自然语言，如「杭州三天怎么玩」")],
        top_k: Annotated[int, Field(description="返回片段数，默认 3")] = 3,
    ) -> dict | str:
        """在本地知识库中按语义检索攻略/游记语料，返回最相关的片段。当需要目的地的深度玩法、避坑经验、行程细节等攻略类信息时使用。"""
        hits = await store.search(current_user_id.get(), query, top_k)
        if not hits:
            # 空分支保留中文短句：改成 dict 后模型会看到 {"hits": [], "images": []}，
            # 那句显式提示就没了
            return "知识库暂无相关内容"
        # 图片单列一份（照 artifacts 的路子，由 agent 侧的侧信道带给前端），并就地给
        # 正文里的图编号：模型照号引用，前端照号取图 —— 拼图片 URL 这件事压根不给模型。
        return {"hits": hits, "images": number_hit_images(hits)}

    return search_knowledge


def register_knowledge_tools(registry, store):
    """把 search_knowledge 注入 ToolRegistry（需要 store，故在 service 层装配时注册）。"""
    registry.register_tool(make_search_knowledge(store), "检索知识库")
