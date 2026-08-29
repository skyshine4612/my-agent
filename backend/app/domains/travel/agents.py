# agents.py —— TravelDomain：注册旅行域的工具与子 Agent
from app.core.agent import Agent
from app.core.registry import ToolRegistry, AgentRegistry
from app.datasource.train_12306 import Train12306DataSource
from app.datasource.flight_variflight import FlightVariflightDataSource
from app.domains.base import Domain
from app.domains.travel import tools, prompts

class TravelDomain(Domain):
    """旅行助手功能域：注册 4 个工具 + 6 个子 Agent"""
    name = "travel"
    required_inputs = ["出发地", "目的地", "出行天数", "预算"]

    def register_tools(self, registry: ToolRegistry, ds):
        """注册旅行域的工具（注入 DataSource）"""
        # 天气工具：注入 ds 的 get_weather，查询城市未来天气
        registry.register("weather_query", "查询城市未来天气",
            {"type":"object","properties":{"city":{"type":"string"},"days":{"type":"integer"}},"required":["city"]},
            tools.make_weather_query(ds))
        # POI 工具：注入 ds 的 search_poi，按关键词搜索景点/酒店/美食
        registry.register("poi_search", "按关键词搜索POI(景点/酒店/美食)",
            {"type":"object","properties":{"keywords":{"type":"string"},"city":{"type":"string"},"price_max":{"type":"number"}},"required":["keywords","city"]},
            tools.make_poi_search(ds))
        # 路线工具：注入 ds 的 plan_route，规划两点间路线
        registry.register("route_plan", "规划两点间路线",
            {"type":"object","properties":{"origin":{"type":"string"},"destination":{"type":"string"},"mode":{"type":"string"}},"required":["origin","destination"]},
            tools.make_route_plan(ds))
        # 预算工具：纯函数工厂，不依赖 ds，核算费用是否超支
        registry.register("budget_calc", "计算预算是否超支",
            {"type":"object","properties":{"items":{"type":"array","items":{"type":"object"}},"total_budget":{"type":"number"}},"required":["items","total_budget"]},
            tools.make_budget_calc())
        # 火车票工具：查询跨城火车票（车次/时间/票价/余票）
        registry.register("train_ticket_query", "查询跨城火车票(车次/时间/票价/余票)",
            {"type":"object","properties":{"date":{"type":"string"},"from_city":{"type":"string"},"to_city":{"type":"string"}},"required":["date","from_city","to_city"]},
            tools.make_train_ticket_query(Train12306DataSource()))
        # 机票工具：查询跨城机票（航班/时间/票价）
        registry.register("flight_query", "查询跨城机票(航班/时间/票价)",
            {"type":"object","properties":{"date":{"type":"string"},"from_city":{"type":"string"},"to_city":{"type":"string"}},"required":["date","from_city","to_city"]},
            tools.make_flight_query(FlightVariflightDataSource()))

    def register_agents(self, registry: AgentRegistry):
        """注册 6 个子 Agent（天气/景点/酒店/美食/预算各绑定工具，规划 Agent 无工具）"""
        specs = [
            # 天气Agent：绑定 weather_query 工具，负责查询目的地天气
            ("天气Agent", prompts.WEATHER_PROMPT, ["weather_query"]),
            # 景点Agent：绑定 poi_search 工具（关键词=景点），负责搜索景点
            ("景点Agent", prompts.POI_PROMPT, ["poi_search"]),
            # 酒店Agent：绑定 poi_search 工具（关键词=酒店），负责推荐酒店
            ("酒店Agent", prompts.HOTEL_PROMPT, ["poi_search"]),
            # 美食Agent：绑定 poi_search 工具（关键词=美食），负责推荐美食
            ("美食Agent", prompts.FOOD_PROMPT, ["poi_search"]),
            # 预算Agent：绑定 budget_calc 工具，负责核算费用并给出超支建议
            ("预算Agent", prompts.BUDGET_PROMPT, ["budget_calc"]),
            # 交通Agent：绑定火车票/机票/路线工具，负责查询跨城交通方式与费用
            ("交通Agent", prompts.TRANSPORT_PROMPT, ["train_ticket_query", "flight_query", "route_plan"]),
            # 规划Agent：无工具，纯 LLM 整合各子 Agent 结果生成 JSON 行程
            ("规划Agent", prompts.PLANNER_AGENT_PROMPT, []),
        ]
        for name, prompt, ts in specs:
            # 规划 Agent 无工具、纯 JSON 生成，开启 json_mode 强制结构化输出；
            # 搜索类 Agent 设 max_iters=3 作为合理上限（允许观察后补充搜索，但防止无限反复）
            registry.register(Agent(name=name, system_prompt=prompt, tools=ts,
                                    json_mode=(name == "规划Agent"),
                                    max_iters=3 if name != "规划Agent" else 10))
