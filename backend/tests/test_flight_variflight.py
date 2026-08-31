# tests/test_flight_variflight.py
# 机票数据源的城市码映射与未收录兜底测试（不依赖网络）
import pytest

from app.datasource.flight_variflight import CITY_IATA, FlightVariflightDataSource


def test_city_iata_covers_key_cities():
    """拉萨等此前缺失的关键城市已有 IATA 码。"""
    assert CITY_IATA["拉萨"] == "LXA"
    assert CITY_IATA["乌鲁木齐"] == "URC"
    assert CITY_IATA["北京"] == "BJS"


def test_to_iata_maps_known_city():
    """已收录城市映射为三字码；已是三字码的输入原样透传。"""
    assert FlightVariflightDataSource._to_iata("拉萨") == "LXA"
    assert FlightVariflightDataSource._to_iata("LXA") == "LXA"


def test_to_iata_unknown_city_returns_original():
    """未收录城市返回原值（中文），交由 search_flights 兜底提示。"""
    assert FlightVariflightDataSource._to_iata("不存在的地方") == "不存在的地方"


@pytest.mark.asyncio
async def test_search_flights_unknown_city_returns_hint():
    """未收录城市在连 MCP 之前返回明确提示，不盲目透传模糊错误。"""
    ds = FlightVariflightDataSource()
    result = await ds.search_flights("2026-09-10", "拉萨", "不存在的地方")
    assert "未收录城市" in result
    assert "不存在的地方" in result
