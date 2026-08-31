# tests/test_amap_web_datasource.py
# 高德 Web API 结果解析单测（只测纯函数，不触碰真实 HTTP）。
from app.datasource.amap_web import parse_poi_list, parse_weather


# ---- POI 解析 ----


def test_parse_poi_list_contract():
    """POI 解析：location 字符串转 {lng,lat}，category 取 type 字段，price 取 biz_ext.cost。"""
    payload = {"pois": [{
        "id": "B001", "name": "宽窄巷子", "address": "成都市青羊区", "location": "104.06,30.68",
        "type": "风景名胜;街区", "biz_ext": {"cost": "50"},
    }]}
    pois = parse_poi_list(payload)
    assert pois[0]["name"] == "宽窄巷子"
    assert pois[0]["location"] == {"lng": 104.06, "lat": 30.68}
    assert pois[0]["category"] == "风景名胜;街区"
    assert pois[0]["price"] == 50.0


def test_parse_poi_list_cost_array():
    """高德 biz_ext.cost 有时是数组，取第一个元素。"""
    payload = {"pois": [{"id": "B", "name": "x", "location": "1,2", "type": "餐饮", "biz_ext": {"cost": ["30"]}}]}
    pois = parse_poi_list(payload)
    assert pois[0]["price"] == 30.0


# ---- 天气解析 ----


def test_parse_weather_contract():
    """天气解析：weather extensions=all 的 forecasts[0].casts[] 解析成每日天气。"""
    payload = {"forecasts": [{"city": "北京市", "casts": [
        {"date": "2026-08-29", "dayweather": "晴", "daytemp": "31", "nighttemp": "22"},
        {"date": "2026-08-30", "dayweather": "多云", "daytemp": "30", "nighttemp": "21"},
    ]}]}
    assert parse_weather(payload) == [
        {"date": "2026-08-29", "day_weather": "晴", "day_temp": 31, "night_temp": 22},
        {"date": "2026-08-30", "day_weather": "多云", "day_temp": 30, "night_temp": 21},
    ]


def test_parse_weather_days_limit():
    """days 参数限制返回天数。"""
    payload = {"forecasts": [{"casts": [
        {"date": "2026-08-29", "dayweather": "晴", "daytemp": "31", "nighttemp": "22"},
        {"date": "2026-08-30", "dayweather": "晴", "daytemp": "30", "nighttemp": "21"},
    ]}]}
    assert len(parse_weather(payload, days=1)) == 1


def test_parse_weather_missing_temp_defaults_to_zero():
    """温度字段缺失时兜底为 0。"""
    payload = {"forecasts": [{"casts": [{"date": "2026-08-29", "dayweather": "晴"}]}]}
    assert parse_weather(payload) == [{"date": "2026-08-29", "day_weather": "晴", "day_temp": 0, "night_temp": 0}]
