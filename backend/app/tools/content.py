# app/tools/content.py
# 结构化工具的 content 渲染：模型看文本，程序从 artifact 拿原始结构。
import json


def render_content(raw):
    """str 原样返回；其余 JSON 序列化，不可序列化对象降级为 str。"""
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, ensure_ascii=False, default=str)
