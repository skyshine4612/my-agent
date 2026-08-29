# app/businesses/__init__.py
# 业务注册中心：把所有可插拔业务实例收进 BUSINESSES 列表，
# 顶层 system prompt 的业务目录与 call_sub_agent 的 enum 都从这里取。
from app.businesses.base import Business
from app.businesses.travel import TravelBusiness

# 已注册的业务列表（每新增一个业务在此追加一个实例）
BUSINESSES: list[Business] = [TravelBusiness()]
