"""入境游客的过境免签行程评估：英文一句话 → 结构化行程 → 地名定位到省市 → 规则引擎判定。

分工：百炼只负责理解与翻译（英文 → 中文国名、口岸、地名）；日期补全、名称归一、
省市定位（高德）和是否合规（app/transit.py）全部由代码完成，结果可复核。
"""

import datetime as dt
import json
import re

from pydantic import BaseModel, Field

from .geo import Geocoder, Place
from .llm import LLM
from .transit import Issue, Rules, Stop, Trip, Verdict, default_rules

PURPOSES = ("旅游", "商务", "访问", "探亲", "工作", "学习", "新闻采访")
CLOCK = re.compile(r"\b\d{1,2}(:\d{2})\s*(am|pm)?\b|\b\d{1,2}\s*(am|pm)\b|\d{1,2}[:：]\d{2}|\d{1,2}\s*点|noon|midnight",
                   re.I)
DEFAULT_TIME = dt.time(12, 0)  # 只说了日期没说时刻时的占位，会写进 assumptions

# 模型偶尔给出全称或旧称，归一到规则表里的国名
COUNTRY_ALIASES = {
    "美利坚合众国": "美国", "英格兰": "英国", "大不列颠": "英国", "苏格兰": "英国",
    "韩国（南韩）": "韩国", "南韩": "韩国", "大韩民国": "韩国", "印尼": "印度尼西亚",
    "俄罗斯联邦": "俄罗斯", "捷克共和国": "捷克", "波斯尼亚和黑塞哥维那": "波黑", "吉尔吉斯": "吉尔吉斯斯坦",
    "阿拉伯联合酋长国": "阿联酋", "新西兰（纽西兰）": "新西兰", "中国香港": "香港", "中国澳门": "澳门",
    "中国台湾": "台湾", "中国": "中国内地",
}

SYSTEM = """你是入境中国旅行助手的需求解析器。用户用英文或其他语言描述来华行程，你把它解析成 JSON（所有值用中文）：
{{"nationality": 护照国籍（中文国名，从下方名单中选；名单里没有就写中文国名；没说填 null）,
 "origin": 飞来中国前所在的国家或地区（中文，没说填 null）,
 "destination": 离开中国后前往的国家或地区（中文，没说填 null；回国就写其国籍国）,
 "destination_city": 离开中国后飞往的城市中文名（如 Seoul → 首尔）或 null,
 "entry_port": 入境的机场或口岸（中文全名如“上海浦东国际机场”，或三字码；只说了城市没说机场就填 null）,
 "entry_city": 入境城市中文名或 null, "exit_city": 离境城市中文名或 null,
 "entry_date": "YYYY-MM-DD" 或 null, "entry_time": "HH:MM"（只有用户写出具体时刻才填，“morning”这类不算）或 null,
 "exit_port": 离境的机场或口岸，同上, "exit_date": "YYYY-MM-DD" 或 null, "exit_time": "HH:MM" 或 null,
 "places": [{{"text": 用户原话, "zh": 中文标准地名（景点用官方中文名，如 Jiuzhaigou → 九寨沟，pandas in Chengdu → 成都大熊猫繁育研究基地）, "city": 所在城市中文名或 null}}],
 "purpose": {purposes} 之一，没说填“旅游”}}
规则：
- 用户没说的一律填 null，不要猜；日期按“今天”推算到最近的未来日期。
- places 包含用户想去的城市和景点，按用户提到的顺序；城市本身也算一项。
- 国籍名单：{countries}
只输出 JSON。"""


class PlaceAsk(BaseModel):
    text: str
    zh: str
    city: str = ""


class TripRequest(BaseModel):
    nationality: str | None = None
    origin: str | None = None
    destination: str | None = None
    destination_city: str | None = None  # 查离境航班用
    entry_port: str | None = None
    entry_time: dt.datetime | None = None
    exit_port: str | None = None
    exit_time: dt.datetime | None = None
    exit_time_known: bool = False  # 用户说了离境时刻（如已订航班）；只给日期时为 False，行程规划会替他查航班
    places: list[PlaceAsk] = Field(default_factory=list)
    purpose: str = "旅游"
    missing: list[str] = Field(default_factory=list)  # 判定前必须向用户追问的字段
    assumptions: list[str] = Field(default_factory=list)  # 代码替用户补的值，页面上要明示


class LocatedStop(BaseModel):
    ask: PlaceAsk
    place: Place | None = None  # None 表示定位失败


class Assessment(BaseModel):
    request: TripRequest
    stops: list[LocatedStop] = Field(default_factory=list)
    verdict: Verdict | None = None  # 缺关键信息时为 None
    rules_as_of: str = ""


def parse_request(llm: LLM, text: str, today: dt.date, rules: Rules | None = None) -> TripRequest:
    rules = rules or default_rules()
    countries = "、".join(sorted(rules.transit_countries | rules.unilateral))
    system = SYSTEM.format(purposes="|".join(PURPOSES), countries=countries)
    raw = llm.chat_json(system, f"今天是 {today.isoformat()}。\n用户：{text}")
    req = TripRequest(
        nationality=_country(raw.get("nationality")),
        origin=_country(raw.get("origin")),
        destination=_country(raw.get("destination")),
        destination_city=_text(raw.get("destination_city")),
        entry_port=_text(raw.get("entry_port")),
        exit_port=_text(raw.get("exit_port")),
        purpose=raw.get("purpose") if raw.get("purpose") in PURPOSES else "旅游",
        places=_places(raw.get("places")),
    )
    req.entry_port = req.entry_port or _port_from_city(rules, _text(raw.get("entry_city")), "入境", req)
    req.exit_port = req.exit_port or _port_from_city(rules, _text(raw.get("exit_city")), "离境", req)
    # 模型常替用户补一个时刻（没说几点却给 09:00），只有原话里有钟点才采用
    clock = bool(CLOCK.search(text))
    req.entry_time = _when(raw.get("entry_date"), raw.get("entry_time") if clock else None, today, "入境",
                           req.assumptions)
    req.exit_time = _when(raw.get("exit_date"), raw.get("exit_time") if clock else None, today, "离境",
                          req.assumptions)
    req.exit_time_known = req.exit_time is not None and not any(a.startswith("离境时刻") for a in req.assumptions)
    if req.origin is None and req.nationality:
        req.origin = req.nationality
        req.assumptions.append(f"未说明从哪里飞来，按国籍国（{req.nationality}）处理")
    # _port_from_city 已经写了带候选机场的追问时，不再重复问一遍笼统的“入境机场”
    req.missing = [label for field, label in _required(rules, req.nationality)
                   if getattr(req, field) is None and not any(m.startswith(label) for m in req.missing)] + req.missing
    return req


def _required(rules: Rules, nationality: str | None) -> list[tuple[str, str]]:
    """判定所需的字段因政策而异：30 天单方面免签不看口岸和下一站。"""
    if nationality is None:
        return [("nationality", "国籍")]
    policy = rules.policy_for(nationality)
    if policy == "not_eligible":
        return []
    need = [("entry_time", "入境日期")]
    if policy == "transit_240h":
        need += [("entry_port", "入境机场"), ("destination", "离开中国后去哪个国家")]
    return need


def _port_from_city(rules: Rules, city: str | None, label: str, req: TripRequest) -> str | None:
    if not city:
        return None
    ports = rules.ports_in_city(city)
    if len(ports) == 1:
        req.assumptions.append(f"{city}只有一个适用口岸，{label}按{ports[0].name}计算")
        return ports[0].name
    if len(ports) > 1:
        options = f"{label}机场（{city}有 {len(ports)} 个：{'、'.join(p.name for p in ports)}）"
        if label == "入境":
            req.missing.append(options)
        else:  # 离境机场不影响能否判定，先不追问，订票时再定
            req.assumptions.append(f"{options}尚未确定，暂不校验离境口岸")
    return None


def locate(geo: Geocoder, places: list[PlaceAsk]) -> list[LocatedStop]:
    return [LocatedStop(ask=p, place=geo.locate(p.zh, p.city)) for p in places]


def assess(llm: LLM, geo: Geocoder, text: str, today: dt.date, rules: Rules | None = None) -> Assessment:
    rules = rules or default_rules()
    req = parse_request(llm, text, today, rules)
    stops = locate(geo, req.places)
    out = Assessment(request=req, stops=stops, rules_as_of=rules.as_of)
    if req.missing:
        return out
    trip = Trip(
        nationality=req.nationality, origin=req.origin or req.nationality, destination=req.destination,
        entry_port=req.entry_port, entry_time=req.entry_time, exit_port=req.exit_port, exit_time=req.exit_time,
        purpose=req.purpose,
        stops=[Stop(name=s.ask.zh, province=s.place.province, city=s.place.city) for s in stops if s.place],
    )
    out.verdict = rules.check(trip)
    # 定位失败不能断言“在区域外”，交给用户确认
    out.verdict.issues += [Issue(rule="R6", level="warn", subject=s.ask.zh,
                                 message=f"没能确定「{s.ask.text}」在哪个省市，请确认它是否在允许停留区域内")
                           for s in stops if s.place is None]
    return out


def _text(v) -> str | None:
    v = str(v).strip() if v is not None else ""
    return v if v and v.lower() not in ("null", "none", "unknown") else None


def _country(v) -> str | None:
    v = _text(v)
    if v is None:
        return None
    v = COUNTRY_ALIASES.get(v, v)
    return v or None


def _places(items) -> list[PlaceAsk]:
    out, seen = [], set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        zh = _text(it.get("zh"))
        if not zh or zh in seen:
            continue
        seen.add(zh)
        out.append(PlaceAsk(text=_text(it.get("text")) or zh, zh=zh, city=_text(it.get("city")) or ""))
    return out[:12]


def _when(date_v, time_v, today: dt.date, label: str, assumptions: list[str]) -> dt.datetime | None:
    try:
        d = dt.date.fromisoformat(str(date_v))
    except ValueError:
        return None
    if d < today:  # 模型把“Oct 26”推到了去年
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:  # 2 月 29 日
            return None
    try:
        t = dt.time.fromisoformat(str(time_v))
    except ValueError:
        t = DEFAULT_TIME
        assumptions.append(f"{label}时刻未说明，暂按 {d:%m-%d} 12:00 计算")
    return dt.datetime.combine(d, t)


def to_json(a: Assessment) -> str:
    return json.dumps(a.model_dump(mode="json"), ensure_ascii=False, indent=1)
