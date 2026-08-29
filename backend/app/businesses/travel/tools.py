# app/businesses/travel/tools.py
# 旅行业务工具工厂：从 domains/travel/tools.py 迁移而来（去掉 budget_calc），
# 每个工厂注入 DataSource 后返回一个 async 工具函数，便于单测替换数据源。
def make_poi_search(ds):
    """POI 搜索工具：按关键词+城市搜索景点/酒店/美食。"""
    async def poi_search(keywords, city, price_max=None):
        """按关键词+城市搜索 POI（景点/酒店/美食），可选价格上限过滤；无结果时返回占位条目。"""
        kw = {"price_max": price_max} if price_max else {}
        pois = await ds.search_poi(keywords, city, **kw)
        return pois or [{"name": "未找到结果", "address": "", "location": None, "category": keywords, "price": 0}]
    return poi_search


def make_weather_query(ds):
    """天气查询工具：查城市未来 N 天天气。"""
    async def weather_query(city, days=3):
        """查询指定城市未来 N 天（默认 3 天）的天气，返回天气列表。"""
        return await ds.get_weather(city, days)
    return weather_query


def make_route_plan(ds):
    """路线规划工具：按交通方式规划两点间路线。"""
    async def route_plan(origin, destination, mode="transit"):
        """按交通方式（默认 transit 公共交通）规划两点间路线。"""
        return await ds.plan_route(origin, destination, mode)
    return route_plan


def make_train_ticket_query(train_ds):
    """火车票查询工具：查出发地到目的地的火车票（车次/时间/票价/余票）。"""
    async def train_ticket_query(date, from_city, to_city):
        """按日期查询出发地到目的地的火车票（车次/时间/票价/余票）。"""
        return await train_ds.search_tickets(date, from_city, to_city)
    return train_ticket_query


def make_flight_query(flight_ds):
    """机票查询工具：查出发地到目的地的机票（航班/时间/票价）。"""
    async def flight_query(date, from_city, to_city):
        """按日期查询出发地到目的地的机票（航班/时间/票价）。"""
        return await flight_ds.search_flights(date, from_city, to_city)
    return flight_query
