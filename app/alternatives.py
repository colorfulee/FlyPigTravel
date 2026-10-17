"""出界景点的替代推荐：百炼按“同类体验”推荐，代码逐个定位并用规则引擎校验区域，不合格的丢弃。"""

from pydantic import BaseModel, Field

from .geo import Geocoder, haversine_m
from .llm import LLM, LLMError
from .route import CITY_EN, short_city
from .transit import Rules, default_rules
from .travelmate import Assessment

MAX_PER_PLACE = 3
MAX_KM = 400  # 离行程中最近城市太远的替代没有意义

SYSTEM = """你是中国旅行顾问。下面这些景点不在 240 小时过境免签允许停留的区域内，外国游客不能去。
请为每个景点推荐最多 5 个“体验最相近”的替代景点，必须在给定的允许城市或省份内，优先靠近游客已经要去的城市。
注意：原景点所在的整个地级市 / 自治州都不允许（例如九寨沟在阿坝州，阿坝州内的黄龙、四姑娘山也都不行）。
输出 JSON：{{"items": [{{"for": 原景点中文名, "zh": 替代景点中文标准名, "city": 所在城市中文名, "en": 英文名, "why": 一句英文说明为什么相似（不超过 18 个词）}}]}}
允许区域（省：城市）：{areas}
游客行程经过的城市：{cities}
只输出 JSON。"""


class Alternative(BaseModel):
    for_place: str  # 被移出的景点（中文）
    zh: str
    en: str
    why: str
    province: str
    city: str
    near_city: str = ""  # 行程中最近的城市（中文简称）
    near_city_en: str = ""
    km: int | None = None


class AlternativeSet(BaseModel):
    items: list[Alternative] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)  # 模型推荐了、但被代码校验拦下的（便于核对）


def suggest(llm: LLM, geo: Geocoder, a: Assessment, rules: Rules | None = None) -> AlternativeSet:
    """只依赖解析与区域判定的结果，不依赖排程，所以可以和查火车航班并行跑。"""
    rules = rules or default_rules()
    out = AlternativeSet()
    if a.verdict is None:
        return out
    blocked = {i.subject for i in a.verdict.issues if i.rule == "R6" and i.level == "block"}
    skipped = [s.ask.zh for s in a.stops if s.ask.zh in blocked]
    # 行程中留下的地点（用来算“离你要去的哪座城最近”）
    route = [(short_city(s.place.city or s.place.province), s.place) for s in a.stops
             if s.place and s.ask.zh not in blocked and s.place.lat is not None]
    if not skipped or not route:
        return out
    areas = "；".join(f"{p}：{'全省' if isinstance(v, str) else '、'.join(v)}" for p, v in rules.areas.items())
    cities = "、".join(dict.fromkeys(name for name, _ in route))
    try:
        located = {s.ask.zh: s.place for s in a.stops if s.place}
        names = "、".join(f"{z}（{located[z].province}{located[z].city}）" if z in located else z for z in skipped)
        raw = llm.chat_json(SYSTEM.format(areas=areas, cities=cities), "景点：" + names)
    except (LLMError, ValueError):
        return out
    count: dict[str, int] = {}
    for item in raw.get("items") or []:
        if not isinstance(item, dict):
            continue
        src, zh = str(item.get("for") or ""), str(item.get("zh") or "").strip()
        if src not in skipped or not zh or count.get(src, 0) >= MAX_PER_PLACE:
            continue
        place = geo.locate(zh, str(item.get("city") or ""))  # 带城市，免得“沙溪古镇”定位到江苏太仓
        if place is None or rules.in_area(place.province, place.city) is not True:
            out.rejected.append(zh)  # 模型推荐错了区域（或定位不到）：不展示
            continue
        near, km = None, None
        if place.lat is not None:
            near, p0 = min(route, key=lambda r: haversine_m(r[1].lat, r[1].lng, place.lat, place.lng))
            km = round(haversine_m(p0.lat, p0.lng, place.lat, place.lng) / 1000)
            if km > MAX_KM:
                out.rejected.append(zh)
                continue
        count[src] = count.get(src, 0) + 1
        out.items.append(Alternative(
            for_place=src, zh=zh, en=str(item.get("en") or zh), why=str(item.get("why") or ""),
            province=place.province, city=place.city, km=km,
            near_city=near or "", near_city_en=CITY_EN.get(near, "") if near else ""))
    return out
