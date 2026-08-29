# app/domains/travel/__init__.py
# 旅行域包：对外暴露 TravelDomain，供可插拔装配时按需导入。
from app.domains.travel.agents import TravelDomain

__all__ = ["TravelDomain"]
