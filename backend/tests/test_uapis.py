# tests/test_uapis.py
# UAPIS 数据源纯函数解析测试（不依赖网络）
from app.datasource.uapis import parse_weather_uapis


def test_parse_weather_uapis():
    """解析 UAPIS weather 返回：forecast 数组 → 每日天气列表，字段映射正确。"""
    payload = {
        "province": "北京市",
        "city": "北京",
        "forecast": [
            {"date": "2026-08-31", "week": "星期一", "weather_day": "晴", "weather_night": "晴",
             "temp_max": 29, "temp_min": 19, "wind_dir_day": "东北风", "wind_scale_day": "2级", "humidity": 45},
            {"date": "2026-09-01", "week": "星期二", "weather_day": "多云", "weather_night": "阴",
             "temp_max": 28, "temp_min": 18, "wind_dir_day": "东南风", "wind_scale_day": "3级", "humidity": 60},
        ],
    }
    result = parse_weather_uapis(payload)
    assert len(result) == 2
    assert result[0]["date"] == "2026-08-31"
    assert result[0]["day_weather"] == "晴"
    assert result[0]["night_weather"] == "晴"
    assert result[0]["day_temp"] == 29
    assert result[0]["night_temp"] == 19
    assert result[0]["wind"] == "东北风2级"


def test_parse_weather_uapis_days_limit():
    """days 参数截断到前 N 天。"""
    payload = {"forecast": [
        {"date": f"2026-09-0{i}", "weather_day": "晴", "weather_night": "晴", "temp_max": 20, "temp_min": 10}
        for i in range(1, 8)
    ]}
    result = parse_weather_uapis(payload, days=3)
    assert len(result) == 3


def test_parse_weather_uapis_invalid():
    """非 dict / 无 forecast 时返回空列表，不抛异常。"""
    assert parse_weather_uapis(None) == []
    assert parse_weather_uapis({}) == []
    assert parse_weather_uapis({"forecast": "not-list"}) == []
