"""把通过区域校验的地点排成按城市的行程，并用飞猪查跨城火车 / 航班和离境航班。

刻意保持简单（10.23 前要稳定）：
- 城市顺序：从入境城市出发，每次去最近的下一个城市（贪心），离境城市放最后
- 每城住几晚：按该城想去的地点数分配，每城至少 1 晚
- 每段交通：先查火车；没有或超过 6 小时再查航班，取到得早的
- 离境航班：用户没订就查一班；必须从适用口岸起飞、在截止时间前，然后整份行程重新跑规则引擎
"""

import datetime as dt
import re

from pydantic import BaseModel, Field

from .flyai import FlyaiClient, FlyaiError
from .geo import Geocoder, haversine_m
from .schemas import Leg
from .transit import Issue, Rules, Stop, Trip, Verdict, default_rules
from .travelmate import Assessment

TRAIN_MAX_MINUTES = 360  # 超过 6 小时的火车再比较一下航班
DAY_START = dt.time(8, 0)  # 换城市当天最早几点出发
EXIT_EARLIEST = dt.time(10, 0)  # 离境当天最早的航班（留出去机场的时间）
LANDING_BUFFER = dt.timedelta(hours=2)  # 落地后出关、去车站
NIGHTS_PER_CITY = 2  # 用户没订离境航班时每城默认住 2 晚

# 自治州名太长，飞猪按城市检索时用简称
CITY_SHORT = {
    "大理白族自治州": "大理", "西双版纳傣族自治州": "西双版纳", "红河哈尼族彝族自治州": "红河",
    "文山壮族苗族自治州": "文山", "楚雄彝族自治州": "楚雄", "迪庆藏族自治州": "迪庆", "阿坝藏族羌族自治州": "阿坝",
}


# 页面英文显示；不在表里的城市显示中文名
CITY_EN = {
    "北京": "Beijing", "天津": "Tianjin", "上海": "Shanghai", "重庆": "Chongqing", "石家庄": "Shijiazhuang",
    "秦皇岛": "Qinhuangdao", "承德": "Chengde", "沈阳": "Shenyang", "大连": "Dalian", "哈尔滨": "Harbin",
    "南京": "Nanjing", "苏州": "Suzhou", "无锡": "Wuxi", "扬州": "Yangzhou", "常州": "Changzhou",
    "杭州": "Hangzhou", "宁波": "Ningbo", "温州": "Wenzhou", "绍兴": "Shaoxing", "嘉兴": "Jiaxing", "舟山": "Zhoushan",
    "金华": "Jinhua", "合肥": "Hefei", "黄山": "Huangshan", "福州": "Fuzhou", "厦门": "Xiamen", "泉州": "Quanzhou",
    "南平": "Nanping", "南昌": "Nanchang", "景德镇": "Jingdezhen", "济南": "Jinan", "青岛": "Qingdao",
    "泰安": "Tai'an", "曲阜": "Qufu", "济宁": "Jining", "烟台": "Yantai", "威海": "Weihai", "郑州": "Zhengzhou",
    "洛阳": "Luoyang", "开封": "Kaifeng", "武汉": "Wuhan", "宜昌": "Yichang", "长沙": "Changsha",
    "张家界": "Zhangjiajie", "广州": "Guangzhou", "深圳": "Shenzhen", "珠海": "Zhuhai", "佛山": "Foshan",
    "南宁": "Nanning", "桂林": "Guilin", "北海": "Beihai", "海口": "Haikou", "三亚": "Sanya", "成都": "Chengdu",
    "乐山": "Leshan", "宜宾": "Yibin", "贵阳": "Guiyang", "安顺": "Anshun", "昆明": "Kunming", "大理": "Dali",
    "丽江": "Lijiang", "西双版纳": "Xishuangbanna", "西安": "Xi'an", "咸阳": "Xianyang", "太原": "Taiyuan",
    "大同": "Datong", "迪庆": "Diqing", "阿坝": "Aba",
    # 常见离境目的地
    "首尔": "Seoul", "东京": "Tokyo", "大阪": "Osaka", "新加坡": "Singapore", "曼谷": "Bangkok", "香港": "Hong Kong",
    "澳门": "Macao", "台北": "Taipei", "吉隆坡": "Kuala Lumpur", "雅加达": "Jakarta", "巴厘岛": "Bali",
    "河内": "Hanoi", "胡志明市": "Ho Chi Minh City", "比什凯克": "Bishkek", "迪拜": "Dubai", "多哈": "Doha",
    "伦敦": "London", "巴黎": "Paris", "法兰克福": "Frankfurt", "莫斯科": "Moscow", "悉尼": "Sydney",
    "洛杉矶": "Los Angeles", "旧金山": "San Francisco", "纽约": "New York", "温哥华": "Vancouver",
    "墨西哥城": "Mexico City",
}


def en(name: str) -> str:
    return CITY_EN.get(name, name)


class CityPlan(BaseModel):
    name: str  # 简称，如“杭州”
    name_en: str = ""
    province: str
    city: str
    lat: float | None = None
    lng: float | None = None
    places: list[str] = Field(default_factory=list)
    arrive: dt.date | None = None
    nights: int = 0


class Itinerary(BaseModel):
    cities: list[CityPlan] = Field(default_factory=list)
    legs: list[Leg | None] = Field(default_factory=list)  # cities[i] → cities[i+1]；None 表示没查到
    exit_leg: Leg | None = None
    exit_leg_booked: bool = False  # True：用户自己给了离境时间，没有替他查
    verdict: Verdict | None = None  # 排完后整份行程重新校验的结论
    issues: list[Issue] = Field(default_factory=list)  # 排程中的提示（如城市太多、没查到车次）
    skipped: list[str] = Field(default_factory=list)  # 因不在允许区域被移出行程的地点
    flyai_calls: int = 0


def short_city(city: str) -> str:
    return CITY_SHORT.get(city) or re.sub(r"(市|地区|盟)$", "", city)


def plan_route(a: Assessment, fly: FlyaiClient, geo: Geocoder, rules: Rules | None = None) -> Itinerary:
    rules = rules or default_rules()
    out = Itinerary()
    req, verdict = a.request, a.verdict
    if verdict is None or verdict.policy == "not_eligible" or req.entry_time is None:
        return out
    blocked = {i.subject for i in verdict.issues if i.rule == "R6" and i.level == "block"}
    out.skipped = [s.ask.zh for s in a.stops if s.ask.zh in blocked]

    cities = _group_cities([s for s in a.stops if s.place and s.ask.zh not in blocked])
    start = _port_city(rules, geo, req.entry_port, cities) or (cities[0] if cities else None)
    if start is None:
        return out
    end = _port_city(rules, geo, req.exit_port, cities)
    order = _order(start, cities, end)
    if end is None:
        order = _ensure_exit_city(rules, order, start)
    out.cities = order

    entry_date = req.entry_time.date()
    target_exit = req.exit_time.date() if req.exit_time else None
    if verdict.policy == "transit_240h" and target_exit and not req.exit_time_known:
        last_day = (verdict.deadline - dt.timedelta(minutes=1)).date()
        if target_exit > last_day:  # 还没订票：直接按最后可离境日排，而不是排一份超期的行程
            out.issues.append(Issue(rule="R5", level="info", subject=f"{target_exit:%m-%d}",
                                    message=f"原定 {target_exit:%m-%d} 离境会超出 240 小时，已改为 {last_day:%m-%d} 离境",
                                    en=f"Leaving on {target_exit:%b %d} would exceed 240 hours, so we planned your "
                                       f"departure for {last_day:%b %d} instead."))
            target_exit = last_day
    if target_exit:
        total = (target_exit - entry_date).days
    elif verdict.policy == "transit_240h":
        total = min((verdict.deadline - dt.timedelta(minutes=1)).date().toordinal() - entry_date.toordinal(),
                    NIGHTS_PER_CITY * max(1, sum(1 for c in order if c.places)))
    else:
        total = NIGHTS_PER_CITY * max(1, sum(1 for c in order if c.places))
    _allocate_nights(order, total, out.issues)

    cursor = entry_date
    for c in order:
        c.arrive = cursor
        cursor += dt.timedelta(days=c.nights)
    exit_date = target_exit or cursor

    for i in range(len(order) - 1):
        a_, b = order[i], order[i + 1]
        day = b.arrive if b.arrive else cursor
        earliest = dt.datetime.combine(day, DAY_START)
        if i == 0 and day == entry_date:
            earliest = max(earliest, req.entry_time + LANDING_BUFFER)
        leg, calls = _best_leg(fly, a_.name, b.name, earliest, out.issues)
        out.flyai_calls += calls
        out.legs.append(leg)

    exit_port, exit_time = req.exit_port, req.exit_time
    # 国籍、下一站、口岸、事由这类问题与日期无关，排什么航班都救不回来，不浪费一次查询
    hopeless = any(i.level == "block" and i.rule in ("R2", "R4", "R8") for i in verdict.issues)
    if req.exit_time_known:
        out.exit_leg_booked = True
    elif req.destination_city and verdict.deadline and not hopeless:
        last_arrival = next((x.arr_time for x in reversed(out.legs) if x and x.arr_time.date() == exit_date), None)
        earliest = max(dt.datetime.combine(exit_date, EXIT_EARLIEST),
                       last_arrival + LANDING_BUFFER if last_arrival else dt.datetime.min)
        leg = _exit_flight(fly, rules, order[-1].name, req.destination_city, earliest, verdict, out)
        if leg:
            out.exit_leg, exit_port, exit_time = leg, leg.dep_code or leg.dep_station, leg.dep_time
            _drop_exit_airport_note(req)

    out.verdict = rules.check(Trip(
        nationality=req.nationality, origin=req.origin or req.nationality, destination=req.destination or "",
        entry_port=req.entry_port or "", entry_time=req.entry_time, exit_port=exit_port, exit_time=exit_time,
        purpose=req.purpose,
        stops=[Stop(name=p, province=c.province, city=c.city) for c in order for p in c.places],
    ))
    return out


def _drop_exit_airport_note(req) -> None:
    """已经替用户选好离境航班，“离境机场尚未确定”的说明就过时了。"""
    keep = [i for i, zh in enumerate(req.assumptions) if not zh.startswith("离境机场")]
    req.assumptions = [req.assumptions[i] for i in keep]
    req.assumptions_en = [req.assumptions_en[i] for i in keep]


def _group_cities(stops) -> list[CityPlan]:
    by_key: dict[tuple[str, str], CityPlan] = {}
    for s in stops:
        p = s.place
        key = (p.province, p.city or p.province)
        if key not in by_key:
            by_key[key] = CityPlan(name=short_city(key[1]), name_en=CITY_EN.get(short_city(key[1]), ""),
                                   province=key[0], city=key[1], lat=p.lat, lng=p.lng)
        c = by_key[key]
        # 用户写的城市本身（如“杭州”）不算一个要去的地点，但它让这座城至少住 1 晚
        if s.ask.zh != c.name:
            c.places.append(s.ask.zh)
        elif not c.places:
            c.places.append(c.name)
    for c in by_key.values():  # 有具体景点后，城市名本身不再算一个地点，免得多分一晚
        if len(c.places) > 1 and c.name in c.places:
            c.places.remove(c.name)
    return list(by_key.values())


def _port_city(rules: Rules, geo: Geocoder, port_text: str | None, cities: list[CityPlan]) -> CityPlan | None:
    """口岸所服务的城市。机场常建在邻市（西安咸阳机场在咸阳），名字以行程中某城开头时就算那座城。"""
    port = rules.find_port(port_text) if port_text else None
    if port is None:
        return None
    for c in cities:
        if port.name.startswith(c.name):
            return c
    place = geo.locate(port.name)
    if place is None:
        return None
    city = place.city or place.province
    return CityPlan(name=short_city(city), name_en=CITY_EN.get(short_city(city), ""), province=place.province,
                    city=city, lat=place.lat, lng=place.lng)


def _same(a: CityPlan, b: CityPlan) -> bool:
    return (a.province, a.city) == (b.province, b.city)


def _dist(a: CityPlan, b: CityPlan) -> float:
    if None in (a.lat, a.lng, b.lat, b.lng):
        return float("inf")
    return haversine_m(a.lat, a.lng, b.lat, b.lng)


def _order(start: CityPlan, cities: list[CityPlan], end: CityPlan | None) -> list[CityPlan]:
    """入境城市打头、离境城市收尾，中间贪心地去最近的城市；用户也想去的入境 / 离境城市合并，不重复。"""
    rest = list(cities)
    head = next((c for c in rest if _same(c, start)), start)
    rest = [c for c in rest if not _same(c, head)]
    tail = None
    if end is not None and not _same(end, head):
        tail = next((c for c in rest if _same(c, end)), end)
        rest = [c for c in rest if not _same(c, tail)]
    elif end is not None and rest:  # 同一口岸进出（广州进、广州出）：最后要回到入境城市
        tail = CityPlan(**head.model_dump(exclude={"places", "arrive", "nights"}))
    order = [head]
    while rest:
        nxt = min(rest, key=lambda c: _dist(order[-1], c))
        order.append(nxt)
        rest.remove(nxt)
    if tail is not None:
        order.append(tail)
    return order


def _ensure_exit_city(rules: Rules, order: list[CityPlan], start: CityPlan) -> list[CityPlan]:
    """没指定离境口岸：最后一城有适用机场就从那里走，否则回到行程里最近一个有机场的城市。"""
    if rules.ports_in_city(order[-1].name):
        return order
    for c in reversed(order[:-1]):
        if rules.ports_in_city(c.name):
            return order + [CityPlan(**c.model_dump(exclude={"places", "arrive", "nights"}))]
    return order + [CityPlan(**start.model_dump(exclude={"places", "arrive", "nights"}))]


def _allocate_nights(order: list[CityPlan], total: int, issues: list[Issue]) -> None:
    """有想去地点的城市每城至少 1 晚，剩下的按地点数用最大余数法分；只是过路的入境 / 离境城市不住。"""
    stay = [c for c in order if c.places] or order[:1]
    if total < len(stay):
        issues.append(Issue(rule="排程", level="warn",
                            message=f"{total} 晚要去 {len(stay)} 个城市，行程很赶，建议减少城市",
                            en=f"{len(stay)} cities in {total} nights is very tight. Consider dropping a city."))
    base = min(total, len(stay))
    for c in stay[:base]:
        c.nights = 1
    left = max(0, total - base)
    if left:
        weights = [len(c.places) for c in stay]
        share = [left * w / sum(weights) for w in weights]
        add = [int(x) for x in share]
        for i in sorted(range(len(stay)), key=lambda i: share[i] - add[i], reverse=True)[:left - sum(add)]:
            add[i] += 1
        for c, n in zip(stay, add):
            c.nights += n


def pick(legs: list[Leg], earliest: dt.datetime, latest: dt.datetime | None = None) -> Leg | None:
    """在时间窗内选：直达优先，其次到得早（换城市当天早点到，下午还能玩），再次便宜。"""
    ok = [x for x in legs if x.dep_time >= earliest and (latest is None or x.dep_time < latest)]
    return min(ok, key=lambda x: (x.transfers, x.arr_time, x.price or 0), default=None)


def _best_leg(fly: FlyaiClient, a: str, b: str, earliest: dt.datetime, issues: list[Issue]) -> tuple[Leg | None, int]:
    calls, train, flight = 0, None, None
    try:
        calls += 1
        train = pick(fly.search_trains(a, b, earliest.date().isoformat(), earliest.hour), earliest)
    except FlyaiError:
        pass
    if train is None or train.minutes > TRAIN_MAX_MINUTES:
        try:
            calls += 1
            flight = pick(fly.search_flights(a, b, earliest.date().isoformat(), earliest.hour), earliest)
        except FlyaiError:
            pass
    best = min([x for x in (train, flight) if x], key=lambda x: x.arr_time, default=None)
    if best is None:
        issues.append(Issue(rule="交通", level="warn", subject=f"{a}→{b}",
                            message=f"{earliest:%m-%d} {a}→{b} 没查到合适的火车或航班，请换一天或手动安排",
                            en=f"No suitable train or flight found for {en(a)} → {en(b)} on {earliest:%b %d}. "
                               "Try another day or book it yourself."))
    return best, calls


def _exit_flight(fly: FlyaiClient, rules: Rules, city: str, dest_city: str, earliest: dt.datetime,
                 verdict: Verdict, out: Itinerary) -> Leg | None:
    """离境航班必须从已核实的适用机场起飞，且在截止时间前。"""
    day = earliest.date()
    out.flyai_calls += 1
    try:
        legs = fly.search_flights(city, dest_city, day.isoformat(), earliest.hour)
    except FlyaiError:  # 飞猪对没有航线的城市对也报错（如丽江→新加坡），按“没找到”处理
        legs = []
    allowed = [x for x in legs if (p := rules.find_port(x.dep_code or x.dep_station)) and p.verified]
    leg = pick(allowed, earliest, verdict.deadline)
    if leg is None:
        out.issues.append(Issue(rule="交通", level="warn", subject=f"{city}→{dest_city}",
                                message=f"{day:%m-%d} 没找到从{city}适用口岸起飞、且在截止时间前的{dest_city}航班",
                                en=f"No flight to {en(dest_city)} found on {day:%b %d} from an eligible airport in {en(city)} "
                                   "before your deadline."))
    return leg
