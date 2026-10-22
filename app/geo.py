"""空间感知：两点距离、用高德把地名定位到省 / 市（过境免签区域校验用）。"""

import math

import httpx
from pydantic import BaseModel

from .config import settings

def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> int:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(2 * r * math.asin(math.sqrt(a)))


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
    """高德 Web 服务：未配置 AMAP_KEY 时 enabled 为 False，所有查询返回 None。"""

    def __init__(self, key: str | None = None, client: httpx.Client | None = None):
        self.key = key if key is not None else settings.amap_key
        self.client = client or httpx.Client(timeout=10)
        self._memo: dict[str, Place | None] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.key)

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
