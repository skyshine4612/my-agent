# tools.py —— 工具工厂函数：注入 DataSource，便于单测替换
def make_poi_search(ds):
    """POI 搜索工具：按关键词+城市搜索景点/酒店/美食"""
    async def poi_search(keywords, city, price_max=None):
        kw = {"price_max": price_max} if price_max else {}
        pois = await ds.search_poi(keywords, city, **kw)
        return pois or [{"name":"未找到结果","address":"","location":None,"category":keywords,"price":0}]
    return poi_search

def make_weather_query(ds):
    """天气查询工具：查城市未来 N 天天气"""
    async def weather_query(city, days=3):
        return await ds.get_weather(city, days)
    return weather_query

def make_route_plan(ds):
    """路线规划工具：按交通方式规划两点间路线"""
    async def route_plan(origin, destination, mode="transit"):
        return await ds.plan_route(origin, destination, mode)
    return route_plan

def make_budget_calc():
    """预算计算工具：核算费用总和是否超预算（纯函数，不依赖 ds）"""
    def budget_calc(items, total_budget):
        total = sum(i["cost"] for i in items)
        return {"total": total, "total_budget": total_budget,
                "within_budget": total <= total_budget,
                "remaining": total_budget - total}
    return budget_calc

def make_train_ticket_query(train_ds):
    """火车票查询工具：查出发地到目的地的火车票（车次/时间/票价/余票）"""
    async def train_ticket_query(date, from_city, to_city):
        return await train_ds.search_tickets(date, from_city, to_city)
    return train_ticket_query

def make_flight_query(flight_ds):
    """机票查询工具：查出发地到目的地的机票（航班/时间/票价）"""
    async def flight_query(date, from_city, to_city):
        return await flight_ds.search_flights(date, from_city, to_city)
    return flight_query
