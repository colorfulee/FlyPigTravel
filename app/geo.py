"""空间感知：距离计算、可选高德地理编码、酒店内 / 步行圈 / 车程圈分层。"""

import base64
import math

import httpx
from pydantic import BaseModel

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


# 高德 POI 大类：这些是商户而非地点，同名时不能用来判断地点在哪个省市
NOT_A_PLACE = {"住宿服务", "餐饮服务", "购物服务", "汽车服务", "汽车销售", "汽车维修", "摩托车服务",
               "商务住宅", "公司企业", "生活服务", "金融保险服务"}


class Place(BaseModel):
    name: str
    province: str
    city: str = ""
    district: str = ""
    lat: float | None = None
    lng: float | None = None
    source: str = ""


def _str(v) -> str:
    # 高德空字段返回 []
    return v if isinstance(v, str) else ""


def _latlng(loc) -> tuple[float | None, float | None]:
    try:
        lng, lat = str(loc).split(",")
        return float(lat), float(lng)
    except ValueError:
        return None, None


class Geocoder:
    """POI 返回只有地址没有坐标时，用高德地理编码补坐标；未配置 AMAP_KEY 则跳过。"""

    def __init__(self, key: str | None = None, client: httpx.Client | None = None):
        self.key = key if key is not None else settings.amap_key
        self.client = client or httpx.Client(timeout=10)
        self._memo: dict[str, tuple[float, float] | Place | None] = {}

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

    def locate(self, name: str, city_hint: str = "") -> Place | None:
        """地名 → 省 / 市（过境免签区域校验用）。

        先用地点搜索：跳过酒店、餐馆、公司等同名商户（“香格里拉”会先搜到香格里拉饭店）；
        再退回地理编码，处理“香格里拉市”“阿坝州”这类行政区名。
        """
        if not self.enabled or not name:
            return None
        k = f"locate|{city_hint}|{name}"
        if k not in self._memo:
            self._memo[k] = self._search_place(name, city_hint) or self._geocode_place(name, city_hint)
        return self._memo[k]

    def _search_place(self, name: str, city_hint: str) -> Place | None:
        try:
            r = self.client.get(
                "https://restapi.amap.com/v3/place/text",
                params={"key": self.key, "keywords": name, "city": city_hint, "offset": 10},
            )
            pois = r.json().get("pois") or []
        except (httpx.HTTPError, ValueError):
            return None
        for p in pois:
            if str(p.get("type", "")).split(";")[0] in NOT_A_PLACE or not p.get("pname"):
                continue
            lat, lng = _latlng(p.get("location"))
            return Place(name=p.get("name") or name, province=p["pname"], city=_str(p.get("cityname")),
                         district=_str(p.get("adname")), lat=lat, lng=lng, source="高德地点搜索")
        return None

    def _geocode_place(self, name: str, city_hint: str) -> Place | None:
        try:
            r = self.client.get(
                "https://restapi.amap.com/v3/geocode/geo",
                params={"key": self.key, "address": name, "city": city_hint},
            )
            geos = r.json().get("geocodes") or []
        except (httpx.HTTPError, ValueError):
            return None
        if not geos or not _str(geos[0].get("province")):
            return None
        g = geos[0]
        lat, lng = _latlng(g.get("location"))
        return Place(name=name, province=g["province"], city=_str(g.get("city")) or g["province"],
                     district=_str(g.get("district")), lat=lat, lng=lng, source="高德地理编码")

    def static_map(self, center: tuple[float, float],
                   points: list[tuple[str, float, float]]) -> tuple[str | None, str | None]:
        """高德静态地图：酒店标“住”，周边按编号标注，视野由高德按全部标注自动适配。

        返回 (data URI, 错误说明)。单张图最多 10 个标注，所以周边最多 9 个。
        """
        if not self.enabled:
            return None, None
        loc = lambda lat, lng: f"{lng:.6f},{lat:.6f}"  # noqa: E731  高德是“经度,纬度”
        markers = [f"mid,0xFF6A00,住:{loc(*center)}"]
        markers += [f"mid,0x059669,{label}:{loc(lat, lng)}" for label, lat, lng in points[:9]]
        try:
            r = self.client.get(
                "https://restapi.amap.com/v3/staticmap",
                params={"key": self.key, "size": "600*400", "scale": 2, "markers": "|".join(markers)},
            )
        except httpx.HTTPError as e:
            return None, f"高德静态地图请求失败：{e}"
        ctype = r.headers.get("content-type", "").split(";")[0]
        if not ctype.startswith("image/"):
            try:
                info = r.json().get("info")
            except ValueError:
                info = r.text[:80]
            return None, f"高德静态地图生成失败：{info}（需要“Web服务”类型的 Key）"
        return f"data:{ctype};base64,{base64.b64encode(r.content).decode()}", None
