"""机票截图识别（多模态）：qwen3-vl-plus 读出每一段航班，代码负责校验、补全日期、找出入境段和离境段。

模型只做“看图抄写”；哪段算入境、哪段算离境、从哪国来、去哪国，都由代码按航段衔接关系判定。
票面时间都是当地时间，入境段的到达、离境段的起飞都在中国，正好是规则引擎要的北京时间。
"""

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

from .llm import LLM, LLMError

MAX_IMAGES = 2
MAINLAND = "中国"
CONNECTION = dt.timedelta(hours=24)  # 24 小时内同一机场换乘算同一段旅程（如洛杉矶→首尔→北京）
MIN_FLIGHT = dt.timedelta(minutes=45)  # 不超过这么短的国际航段，多半是把登机时间当成了起飞时间

PROMPT = """这是一张机票相关截图（电子客票行程单、订单确认页或登机牌）。逐段抄写其中的航班，不要推测图里没有的内容。
输出 JSON：{"is_ticket": 是否机票相关截图,
 "segments": [{"flight_no": 航班号如 MU586,
   "dep_airport": 出发机场名（照抄）, "dep_iata": 出发机场三字码或 null, "dep_city": 出发城市中文名, "dep_country": 出发国家或地区中文名（中国内地写“中国”，香港/澳门/台湾分别写“香港”“澳门”“台湾”）,
   "dep_date": "YYYY-MM-DD", "dep_time": "HH:MM" 或 null,
   "arr_airport": 到达机场名, "arr_iata": 到达机场三字码或 null, "arr_city": 到达城市中文名, "arr_country": 到达国家或地区中文名,
   "arr_date": "YYYY-MM-DD" 或 null（图里没写到达日期就填 null）, "arr_time": "HH:MM" 或 null（没写就填 null）}]}
注意：登机牌上的 BOARDING（登机时间）不是起飞时间，起飞时间是 DEPARTURE；登机牌通常没有到达时间，就填 null。
年份没写时，按“今天是 {today}”推算为最近的未来日期。"""


class Segment(BaseModel):
    flight_no: str
    dep_airport: str = ""
    dep_iata: str = ""
    dep_city: str = ""
    dep_country: str = ""
    dep_time: dt.datetime
    dep_time_known: bool = True  # 票面写了起飞时刻
    arr_airport: str = ""
    arr_iata: str = ""
    arr_city: str = ""
    arr_country: str = ""
    arr_time: dt.datetime | None = None  # 登机牌通常只印起飞时刻


class TicketInfo(BaseModel):
    segments: list[Segment] = Field(default_factory=list)
    entry: Segment | None = None  # 飞抵中国内地的那段
    exit: Segment | None = None  # 从中国内地飞走的那段
    origin: str | None = None  # 整段旅程从哪国出发（经第三地转机时取最初出发地）
    destination: str | None = None  # 离开中国后最终去哪国
    destination_city: str | None = None
    notes: list[str] = Field(default_factory=list)  # 中文说明
    notes_en: list[str] = Field(default_factory=list)
    unreadable: int = 0  # 认不出航班的图片数

    def note(self, zh: str, en: str) -> None:
        self.notes.append(zh)
        self.notes_en.append(en)


def read_tickets(llm: LLM, images: list[str], today: dt.date) -> TicketInfo:
    images = images[:MAX_IMAGES]
    prompt = PROMPT.replace("{today}", today.isoformat())
    with ThreadPoolExecutor(max_workers=MAX_IMAGES) as pool:
        raws = list(pool.map(lambda img: _safe_vision(llm, img, prompt), images))
    info = TicketInfo()
    seen = set()
    for raw in raws:
        segs = [s for s in (_segment(x, today) for x in (raw or {}).get("segments") or []) if s]
        if not segs:
            info.unreadable += 1
        for s in segs:
            key = (s.flight_no, s.dep_time.date())
            if key not in seen:  # 两张截图里同一段航班只算一次
                seen.add(key)
                info.segments.append(s)
    info.segments.sort(key=lambda s: s.dep_time)
    _resolve(info)
    return info


def _safe_vision(llm: LLM, image: str, prompt: str) -> dict | None:
    try:
        return llm.vision_json(image, prompt)
    except (LLMError, ValueError):
        return None


FLIGHT_NO = re.compile(r"^[A-Z0-9]{2}\d{1,4}[A-Z]?$")


def _segment(x, today: dt.date) -> Segment | None:
    """模型抄错或编造的条目在这里丢掉：航班号格式、日期必须合法，起飞不能晚于到达。"""
    if not isinstance(x, dict):
        return None
    no = re.sub(r"\s", "", str(x.get("flight_no") or "")).upper()
    dep_d = _date(x.get("dep_date"), today)
    if not FLIGHT_NO.match(no) or dep_d is None:
        return None
    dep_t = _time(x.get("dep_time"))
    dep = dt.datetime.combine(dep_d, dep_t or dt.time(12, 0))
    arr = None
    arr_t = _time(x.get("arr_time"))
    if arr_t is not None:
        arr_d = _date(x.get("arr_date"), today) or dep_d
        arr = dt.datetime.combine(arr_d, arr_t)
        if arr < dep - dt.timedelta(hours=20):  # 向西飞可能“早到”，但不会早将近一天
            arr = None
        elif dep <= arr <= dep + MIN_FLIGHT:  # 实测：登机牌的 12:40 登机 / 13:20 起飞被读成起飞 / 到达
            arr = None
    return Segment(
        flight_no=no, dep_airport=_s(x.get("dep_airport")), dep_iata=_iata(x.get("dep_iata")),
        dep_city=_s(x.get("dep_city")), dep_country=_country(x.get("dep_country")), dep_time=dep,
        dep_time_known=dep_t is not None, arr_airport=_s(x.get("arr_airport")), arr_iata=_iata(x.get("arr_iata")),
        arr_city=_s(x.get("arr_city")), arr_country=_country(x.get("arr_country")), arr_time=arr,
    )


def _resolve(info: TicketInfo) -> None:
    segs = info.segments
    entry_i = next((i for i, s in enumerate(segs) if s.arr_country == MAINLAND and s.dep_country != MAINLAND), None)
    exit_i = next((i for i in range(len(segs) - 1, -1, -1)
                   if segs[i].dep_country == MAINLAND and segs[i].arr_country != MAINLAND), None)
    if entry_i is not None:
        info.entry = segs[entry_i]
        i = entry_i
        while i > 0 and _connects(segs[i - 1], segs[i]):  # 往前找转机的起点：洛杉矶→首尔→北京 的出发地是美国
            i -= 1
        info.origin = segs[i].dep_country or None
        if i < entry_i:
            info.note(f"经{segs[entry_i].dep_city or segs[entry_i].dep_country}转机入境，出发地按{info.origin}计算",
                      f"You connect in {segs[entry_i].dep_city or 'another city'} on the way in, "
                      f"so your trip starts from {info.origin}.")
    if exit_i is not None:
        info.exit = segs[exit_i]
        j = exit_i
        while j + 1 < len(segs) and _connects(segs[j], segs[j + 1]):
            j += 1
        info.destination = segs[j].arr_country or None
        info.destination_city = segs[j].arr_city or None
    if info.entry and info.entry.arr_time is None:
        info.note(f"截图里没有 {info.entry.flight_no} 的到达时刻，入境时间按起飞日期估算",
                  f"Your screenshot doesn't show when {info.entry.flight_no} lands, so we used its departure date.")


def _connects(a: Segment, b: Segment) -> bool:
    same_airport = (a.arr_iata and a.arr_iata == b.dep_iata) or (a.arr_city and a.arr_city == b.dep_city)
    land = a.arr_time or a.dep_time
    return bool(same_airport) and dt.timedelta(0) <= b.dep_time - land <= CONNECTION


def _s(v) -> str:
    return str(v).strip() if v not in (None, "null") else ""


def _iata(v) -> str:
    v = _s(v).upper()
    return v if re.fullmatch(r"[A-Z]{3}", v) else ""


COUNTRY_FIX = {"中国内地": MAINLAND, "中国大陆": MAINLAND, "中华人民共和国": MAINLAND, "中国香港": "香港",
               "中国澳门": "澳门", "中国台湾": "台湾", "美利坚合众国": "美国", "韩国（南韩）": "韩国", "大韩民国": "韩国"}


def _country(v) -> str:
    v = _s(v)
    return COUNTRY_FIX.get(v, v)


def _date(v, today: dt.date) -> dt.date | None:
    try:
        d = dt.date.fromisoformat(str(v))
    except ValueError:
        return None
    if d < today - dt.timedelta(days=2):  # 年份推错（票面常只写 26OCT）
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d


def _time(v) -> dt.time | None:
    try:
        return dt.time.fromisoformat(str(v).strip()[:5])
    except ValueError:
        return None
