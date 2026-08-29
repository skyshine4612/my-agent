# app/domains/__init__.py
# 功能域包：对外暴露 Domain 抽象，供各业务域（旅行、天气等）可插拔接入。
from app.domains.base import Domain

__all__ = ["Domain"]
