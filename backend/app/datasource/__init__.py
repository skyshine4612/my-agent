# app/datasource/__init__.py
# 数据源包：对外暴露 DataSource 抽象契约与高德 MCP 数据源实现。
from app.datasource.base import DataSource
from app.datasource.amap_mcp import AmapMcpDataSource

__all__ = ["DataSource", "AmapMcpDataSource"]
