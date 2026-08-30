# app/datasource/base.py
# DataSource（数据源）抽象：定义后端获取外部数据（POI、天气）的统一契约。
# 各数据源实现（如高德 MCP）只需继承本抽象并实现契约方法，
# 业务侧（Agent / 工具）即可无感切换数据来源。
from abc import ABC, abstractmethod


class DataSource(ABC):
    """数据源抽象基类：约定两个数据获取方法的输入输出形状。

    契约结构：
        - search_poi：返回 POI 列表，每项含 name/address/category/price
        - get_weather：返回每日天气列表，每项含 date/day_weather/day_temp/night_temp
    """

    @abstractmethod
    async def search_poi(self, keywords, city, **kw) -> list[dict]:
        """按关键字+城市搜索地点，返回 POI 列表。"""
        ...

    @abstractmethod
    async def get_weather(self, city, days) -> list[dict]:
        """查询城市未来天气，返回每日天气列表。"""
        ...
