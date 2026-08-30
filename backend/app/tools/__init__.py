# app/tools/__init__.py
# 统一工具注册：把所有工具（旅行 / 系统 / 网络 / 新闻）注入同一个 ToolRegistry。
from app.tools import network, news, system, travel


def register_all_tools(registry, amap_ds, skill_names):
    """把所有工具注册到 ToolRegistry（只在启动时调用一次）。

    参数：
        registry:    ToolRegistry，工具注册表
        amap_ds:     高德数据源（旅行工具 poi_search / weather_query 共用）
        skill_names: 业务名列表（get_skill 的 enum）
    """
    travel.register_travel_tools(registry, amap_ds)
    system.register_system_tools(registry, skill_names)
    network.register_network_tools(registry)
    news.register_news_tools(registry)
