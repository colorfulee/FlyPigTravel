"""空间感知：距离计算、可选高德地理编码、酒店内 / 步行圈 / 车程圈分层。"""

import math

import httpx

from .config import settings

WALK_MAX_M = 1500  # 约步行 20 分钟
DRIVE_MAX_M = 20000  # 约车程 30-40 分钟（市区）


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


def ring_for(distance_m: int | None) -> tuple[str, int | None]:
    """返回 (分层, 预估分钟)。步行按 80m/分钟，车程按市区 25km/h 粗估。"""
    if distance_m is None:
        return "距离待确认", None
    if distance_m <= WALK_MAX_M:
        return "步行圈", max(1, round(distance_m / 80))
    if distance_m <= DRIVE_MAX_M:
        return "车程圈", max(5, round(distance_m / 25000 * 60) + 5)
    return "车程圈", round(distance_m / 40000 * 60) + 10


def bearing_deg(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """从酒店指向 POI 的方位角（0=北，顺时针），用于前端空间图。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


class Geocoder:
    """POI 返回只有地址没有坐标时，用高德地理编码补坐标；未配置 AMAP_KEY 则跳过。"""

    def __init__(self, key: str | None = None, client: httpx.Client | None = None):
        self.key = key if key is not None else settings.amap_key
        self.client = client or httpx.Client(timeout=10)
        self._memo: dict[str, tuple[float, float] | None] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def geocode(self, address: str, city: str) -> tuple[float, float] | None:
        if not self.enabled or not address:
            return None
        k = f"{city}|{address}"
        if k not in self._memo:
            self._memo[k] = self._query(address, city)
        return self._memo[k]

    def _query(self, address: str, city: str) -> tuple[float, float] | None:
        try:
            r = self.client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": self.key, "address": address, "city": city},
            )
            geos = r.json().get("geocodes") or []
            if not geos:
                return None
            lng, lat = geos[0]["location"].split(",")
            # 高德为 GCJ-02；飞猪坐标系待实测确认，即便不同系，几百米偏差也不影响分层量级
            return float(lat), float(lng)
        except (httpx.HTTPError, KeyError, ValueError):
            return None
