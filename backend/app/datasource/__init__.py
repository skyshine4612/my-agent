# app/datasource/__init__.py
# 数据源包：对外暴露高德 Web API 数据源实现。
from app.datasource.amap_web import AmapWebDataSource

__all__ = ["AmapWebDataSource"]
