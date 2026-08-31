# datasource/flight_variflight.py
"""variflight 机票数据源：查询跨城航班与票价。"""
from app.config import settings
from app.datasource.mcp_base import McpDataSource

# 中文城市名 → IATA 三字码。variflight 的 getFlightPriceByCities 需要城市码（city code）：
# 多机场城市用城市码（北京 BJS / 上海 SHA / 西安 SIA），单机场城市用机场码（即城市码）。
# 数据来源：IATA 公开标准城市码/机场码，覆盖省会、直辖市、计划单列市与主要旅游/出行城市。
CITY_IATA = {
    # 直辖市
    "北京": "BJS", "上海": "SHA", "天津": "TSN", "重庆": "CKG",
    # 华北
    "石家庄": "SJW", "太原": "TYN", "呼和浩特": "HET", "大同": "DAT", "运城": "YCU",
    "包头": "BAV", "鄂尔多斯": "DSN", "秦皇岛": "BPE",
    # 东北
    "沈阳": "SHE", "大连": "DLC", "长春": "CGQ", "哈尔滨": "HRB",
    "延吉": "YNJ", "牡丹江": "MDG", "齐齐哈尔": "NDG", "大庆": "DQA", "佳木斯": "JMU",
    # 华东
    "南京": "NKG", "杭州": "HGH", "宁波": "NGB", "温州": "WNZ", "无锡": "WUX",
    "常州": "CZX", "徐州": "XUZ", "南通": "NTG", "合肥": "HFE",
    "福州": "FOC", "厦门": "XMN", "泉州": "JJN", "武夷山": "WUS",
    "南昌": "KHN", "赣州": "KOW", "景德镇": "JDZ", "上饶": "SQD",
    "济南": "TNA", "青岛": "TAO", "烟台": "YNT", "威海": "WEH", "临沂": "LYI",
    "义乌": "YIW", "舟山": "HSN", "台州": "HYN",
    # 华中
    "郑州": "CGO", "武汉": "WUH", "长沙": "CSX",
    "洛阳": "LYA", "宜昌": "YIH", "襄阳": "XFN", "十堰": "WDS", "恩施": "ENH",
    "张家界": "DYG", "常德": "CGD", "衡阳": "HNY",
    # 华南
    "广州": "CAN", "深圳": "SZX", "珠海": "ZUH", "汕头": "SWA", "湛江": "ZHA",
    "海口": "HAK", "三亚": "SYX",
    "南宁": "NNG", "桂林": "KWL", "北海": "BHY", "柳州": "LZH",
    # 西南
    "成都": "CTU", "贵阳": "KWE", "昆明": "KMG",
    "拉萨": "LXA", "丽江": "LJG", "大理": "DLU", "西双版纳": "JHG", "香格里拉": "DIG",
    "绵阳": "MIG", "遵义": "ZYI", "泸州": "LZO", "宜宾": "YBP", "西昌": "XIC",
    "攀枝花": "PZI", "腾冲": "TCZ", "保山": "BSD", "芒市": "LUM",
    "铜仁": "TEN", "兴义": "ACX", "稻城": "DCY", "九寨沟": "JZH", "康定": "KGT", "泸沽湖": "NLH",
    # 西北
    "西安": "SIA", "兰州": "LHW", "西宁": "XNN", "银川": "INC",
    "乌鲁木齐": "URC", "喀什": "KHG", "库尔勒": "KRL", "阿克苏": "AKU", "伊宁": "YIN",
    "和田": "HTN", "克拉玛依": "KRY", "敦煌": "DNH", "嘉峪关": "JGN", "张掖": "YZY",
    "榆林": "UYN", "延安": "ENY", "汉中": "HZG",
}


class FlightVariflightDataSource(McpDataSource):
    """variflight 机票数据源：直连 variflight 官方 MCP（不走 ModelScope 代理）。

    认证用 X-API-Key header（区别于 ModelScope 托管 MCP 的 Bearer 令牌）。
    """

    def __init__(self):
        super().__init__(settings.flight_variflight_url, headers={"X-API-Key": settings.variflight_api_key})

    @staticmethod
    def _to_iata(city: str) -> str:
        """中文城市名 → IATA 3 字母城市代码；查不到时返回原值（可能是用户已给代码）。"""
        return CITY_IATA.get(city, city)

    async def search_flights(self, date: str, from_city: str, to_city: str) -> str:
        """查跨城机票（含舱位价格），用 getFlightPriceByCities 返回航班号/时间 + 各舱位票价。

        未收录的城市（中文名无法映射到三字码）直接返回明确提示，避免 variflight 报模糊的「格式不正确」。
        """
        dep = self._to_iata(from_city)
        arr = self._to_iata(to_city)
        # 映射后仍含非 ASCII 字符（中文），说明该城市未收录、没有三字码
        if any(ord(ch) > 127 for ch in dep) or any(ord(ch) > 127 for ch in arr):
            return f"未收录城市（出发={from_city}，到达={to_city}），请改用 IATA 三字码查询，或换用已支持的城市。"
        result = await self._call_tool("getFlightPriceByCities", {
            "dep_city": dep,
            "arr_city": arr,
            "dep_date": date})
        return self._extract_text(result)
