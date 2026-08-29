# app/core/planner.py
# 任务规划器（Planner）：先判断用户请求是否包含某功能所需的关键信息（required_inputs，
# 由 Domain 声明的通用参数），信息齐全则让 LLM 动态拆解出子任务 DAG，缺失则返回澄清问题清单。
import json

# 注意：提示词里的 JSON 示例需把花括号加倍（{{ }}）转义，否则会被 str.format 当作占位符解析；
# 只有 {required_inputs} 与 {agents} 两个真正的占位符保持单层花括号，供 format 注入。
PLANNER_PROMPT = """你是任务规划器。先判断用户请求是否包含该功能所需的关键信息：{required_inputs}。
- 信息齐全 → 输出 {{"tasks":[{{"id":"T1","agent":"...","params":{{}},"depends_on":[]}}]}}
- 缺任一项 → 输出 {{"clarify":true,"questions":["..."]}}（只问缺失项，一次问齐）
可用 Agent：
{agents}
只输出 JSON。"""


class Planner:
    """任务规划器：判断信息完整性，齐全则动态拆解任务，缺失则返回澄清问题。"""
    def __init__(self, llm, agent_registry, required_inputs):
        self.llm = llm; self.agents = agent_registry; self.required_inputs = required_inputs

    async def plan(self, user_message):
        """根据用户消息生成执行计划，返回 {"tasks":[...]} 或 {"clarify":true,"questions":[...]}。

        澄清判断：把 required_inputs 通用参数与 Agent 能力清单注入 PLANNER_PROMPT，
        交给 LLM 判断信息是否齐全——齐全则拆解出 tasks，缺失则返回 clarify 与缺失项问题。
        """
        resp = await self.llm.chat([
            {"role": "system", "content": PLANNER_PROMPT.format(
                required_inputs="、".join(self.required_inputs),
                agents=self.agents.describe())},
            {"role": "user", "content": user_message}],
            # 强制 JSON 输出：从源头让模型只返回合法 JSON，避免尾随文本导致解析失败
            response_format={"type": "json_object"})
        # 解析 LLM 输出的 JSON，得到任务清单或澄清问题清单
        return self._parse(resp["content"])   # dict：{"tasks":[...]} 或 {"clarify":true,"questions":[...]}

    def _parse(self, text):
        """从 LLM 输出中提取首个完整 JSON 对象，忽略其后的尾随文本。

        用 json.JSONDecoder().raw_decode 从第一个 { 开始解析第一个完整 JSON 对象，
        自动停在对象结尾；避免贪婪正则把尾随的额外 JSON/文本也包进来导致 Extra data 报错。
        """
        start = text.find("{")
        if start == -1:
            return {"clarify": True, "questions": ["请补充关键信息"]}
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj
        except json.JSONDecodeError:
            return {"clarify": True, "questions": ["请补充关键信息"]}
