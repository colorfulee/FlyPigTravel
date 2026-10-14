"""「住进去」主流程：理解需求 → 召回酒店与套餐 → 挖掘酒店内外体验 → 编排旅程与主动提醒。

分工原则：百炼负责理解与表达；价格、距离、时间窗口与来源校验全部由代码完成，
模型输出里引用不到真实数据的内容一律丢弃。
"""

import datetime as dt
import json
import re
from concurrent.futures import ThreadPoolExecutor

from .flyai import FlyaiClient, FlyaiError
from .geo import Geocoder, bearing_deg, haversine_m, ring_for
from .llm import LLM, LLMError
from .schemas import (
    PERSONAS, Experience, Hotel, Intent, Journey, Package, PlanResponse, Poi, Reminder,
    TimelineItem, ToConfirm,
)

# 人群 → 优先查询的周边 POI 类型（取自 flyai search-poi 的合法分类）
PERSONA_POI = {
    "亲子": ["主题乐园", "海洋馆", "动物园", "博物馆"],
    "情侣": ["城市观光", "地标建筑", "剧院剧场", "文创街区"],
    "独处": ["博物馆", "展览馆", "园林花园"],
    "闺蜜": ["文创街区", "市集", "地标建筑"],
    "带父母": ["园林花园", "历史古迹", "温泉"],
}
WANT_POI = {"温泉": "温泉", "露营": "露营", "滑雪": "滑雪", "乐园": "主题乐园", "博物馆": "博物馆",
            "看展": "展览馆", "演出": "剧院剧场", "古镇": "古镇古村", "海边": "沙滩海岛"}
PERSONA_WORDS = {
    "亲子": ["亲子", "儿童", "家庭", "乐园", "萌宠", "kids"],
    "情侣": ["浪漫", "景观", "夜景", "下午茶", "情侣", "纪念日", "浴缸"],
    "独处": ["安静", "书", "SPA", "健身", "景观", "设计"],
    "闺蜜": ["出片", "下午茶", "网红", "拍照", "泳池", "酒吧"],
    "带父母": ["早餐", "安静", "交通便利", "中餐", "园林", "无障碍"],
}
CONTRAST = {"亲子": "情侣", "情侣": "亲子", "独处": "闺蜜", "闺蜜": "独处", "带父母": "亲子"}

CHECK_IN = "14:00"
DEFAULT_CHECK_OUT = "12:00"


# ---------------------------------------------------------------- 1. 理解需求
INTENT_SYSTEM = """你是酒店微度假规划助手的需求解析器。把用户的一句话需求解析为 JSON：
{"city": 城市名, "check_in": "YYYY-MM-DD", "check_out": "YYYY-MM-DD",
 "persona": "亲子|情侣|独处|闺蜜|带父母" 之一, "budget": 每晚预算整数或 null, "min_star": 用户明确说出的最低星级（五星/豪华=5，四星/高档=4），未明说则 null,
 "wants": [想要的体验关键词], "hotel_hint": 用户点名的酒店名或 null}
规则：日期未说明时填 null；“这周末”指最近的周六入住、周日退房；人群无法判断时填“情侣”。只输出 JSON。"""


def next_saturday(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=(5 - today.weekday()) % 7 or 7)


def parse_intent(llm: LLM, text: str, today: dt.date) -> Intent:
    raw = llm.chat_json(INTENT_SYSTEM, f"今天是 {today.isoformat()}（星期{'一二三四五六日'[today.weekday()]}）。\n用户需求：{text}")
    ci = _date(raw.get("check_in"))
    co = _date(raw.get("check_out"))
    if not ci or ci < today:
        ci = next_saturday(today)
    if not co or co <= ci:
        co = ci + dt.timedelta(days=1)
    persona = raw.get("persona") if raw.get("persona") in PERSONAS else "情侣"
    budget = raw.get("budget")
    min_star = raw.get("min_star")
    return Intent(
        city=(raw.get("city") or "上海").strip(),
        check_in=ci.isoformat(), check_out=co.isoformat(), persona=persona,
        budget=int(budget) if isinstance(budget, (int, float)) and budget > 0 else None,
        min_star=int(min_star) if isinstance(min_star, (int, float)) and 1 <= min_star <= 5 else None,
        wants=[str(w) for w in (raw.get("wants") or [])][:6],
        hotel_hint=raw.get("hotel_hint") or None,
    )


def _date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v))
    except ValueError:
        return None


# ---------------------------------------------------------------- 2. 召回与排序
def recall(fly: FlyaiClient, intent: Intent, warnings: list[str]) -> tuple[list[Hotel], list[Package]]:
    hotels: dict[str, Hotel] = {}
    max_price = int(intent.budget * 1.3) if intent.budget else None
    stars = "5" if intent.min_star == 5 else "4,5"
    searches = (
        lambda: fly.search_hotels(intent.city, intent.check_in, intent.check_out, max_price=max_price, stars=stars),
        lambda: fly.search_marriott_hotels(intent.city, intent.check_in, intent.check_out, max_price=max_price),
    )
    if intent.hotel_hint:  # 点名的酒店不一定在前几条结果里，单独按名称搜
        searches = (lambda: fly.search_hotels(intent.city, intent.check_in, intent.check_out,
                                              keywords=intent.hotel_hint, stars=None),) + searches
    for search in searches:
        try:
            for h in search():
                hotels.setdefault(h.name, h)
        except FlyaiError as e:
            warnings.append(str(e))
    try:
        packages = fly.search_packages(intent.city)
    except FlyaiError as e:
        warnings.append(str(e))
        packages = []
    return list(hotels.values()), packages


def packages_of(hotel: Hotel, packages: list[Package]) -> list[Package]:
    def match(p: Package) -> bool:
        if p.hotel_name:
            return p.hotel_name in hotel.name or hotel.name in p.hotel_name
        return hotel.name in p.name  # 线上数据无 hotelName，酒店名写在套餐标题里

    return [p for p in packages if match(p)]


# 飞猪 star 字段是档次文案而非数字
STAR_LEVEL = {"五星级": 5, "豪华型": 5, "四星级": 4, "高档型": 4, "三星级": 3, "舒适型": 3}


def star_level(h: Hotel) -> int | None:
    return STAR_LEVEL.get(h.star)


def rank_hotels(hotels: list[Hotel], packages: list[Package], intent: Intent) -> list[Hotel]:
    words = PERSONA_WORDS.get(intent.persona, []) + intent.wants

    def score(h: Hotel) -> float:
        s = (h.score or 4.5) * 10
        level = star_level(h)
        s += 8 if level == 5 else 0  # 高品质 staycation：同等条件优先豪华/五星
        if intent.min_star and level is not None and level < intent.min_star:
            s -= 40
        pk = packages_of(h, packages)
        s += min(len(pk), 3) * 5  # 有套餐 = 有可挖掘的体验
        text = " ".join([h.name, h.review, h.interests_poi] + [p.name + p.selling_point for p in pk])
        s += sum(4 for w in words if w and w.lower() in text.lower())
        if intent.hotel_hint and intent.hotel_hint in h.name:
            s += 1000
        if intent.budget:
            if h.price is None:
                s -= 5
            elif h.price > intent.budget:
                s -= 30 * (h.price / intent.budget - 1) + 10
        return s

    return sorted(hotels, key=score, reverse=True)


# ---------------------------------------------------------------- 3. 体验挖掘
MINE_SYSTEM = """你从飞猪酒店数据中提取“住客可以体验的具体项目”。输入是酒店的套餐名称、套餐卖点和用户评价原文。
输出 JSON：{"items": [{"title": 体验名(≤12字), "detail": 一句话怎么玩, "source": "套餐权益|用户评价",
 "evidence": 原文中支撑它的连续片段(必须逐字摘抄), "personas": [适合人群，取自 亲子/情侣/独处/闺蜜/带父母]}]}
只提取原文明确提到的内容，不要补充常识或猜测。最多 10 条。"""

VISION_PROMPT = """这是一张酒店图片。列出画面中能看到的、住客可能体验的设施或场景（如无边泳池、江景、下午茶、儿童乐园、浴缸）。
输出 JSON：{"items": [{"title": 设施名(≤10字), "evidence": 你在画面中看到的依据(一句话)}]}。看不清就返回空列表，不要猜。"""


def mine_hotel_experiences(llm: LLM, hotel: Hotel, pkgs: list[Package]) -> list[Experience]:
    corpus_parts = [f"套餐：{p.name}｜卖点：{p.selling_point}" for p in pkgs] + (
        [f"用户评价：{hotel.review}"] if hotel.review else [])
    if not corpus_parts:
        return []
    corpus = "\n".join(corpus_parts)
    raw = llm.chat_json(MINE_SYSTEM, f"酒店：{hotel.name}\n{corpus}")
    out = []
    for i, it in enumerate(raw.get("items") or []):
        ev = str(it.get("evidence") or "").strip()
        src = it.get("source") if it.get("source") in ("套餐权益", "用户评价") else None
        # 反幻觉：证据必须能在原文中逐字找到，否则丢弃
        if not ev or not src or ev not in corpus or not it.get("title"):
            continue
        url = next((p.url for p in pkgs if ev in p.name or ev in p.selling_point), hotel.url)
        out.append(Experience(
            id=f"h{i}", title=str(it["title"])[:16], detail=str(it.get("detail") or ""),
            source=src, evidence=ev, ring="酒店内", url=url,
            personas=[p for p in it.get("personas") or [] if p in PERSONAS],
        ))
    return out


def mine_image_experiences(llm: LLM, images: list[str], known_titles: set[str]) -> list[Experience]:
    out: list[Experience] = []

    def look(url: str) -> list[dict]:
        try:
            return llm.vision_json(url, VISION_PROMPT).get("items") or []
        except (LLMError, json.JSONDecodeError):
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(look, images))
    for url, items in zip(images, results):
        for it in items:
            title = str(it.get("title") or "").strip()[:12]
            if not title or title in known_titles:
                continue
            known_titles.add(title)
            out.append(Experience(
                id=f"v{len(out)}", title=title, detail="从酒店图片识别，开放情况需确认",
                source="图片识别", evidence=str(it.get("evidence") or ""), ring="酒店内", url=url,
            ))
    return out[:5]


def nearby_experiences(fly: FlyaiClient, geo: Geocoder, hotel: Hotel, intent: Intent,
                       personas: list[str], warnings: list[str]) -> list[Experience]:
    cats: list[str] = []
    for w in intent.wants:
        for k, c in WANT_POI.items():
            if k in w and c not in cats:
                cats.append(c)
    for p in personas:
        for c in PERSONA_POI[p][:2]:
            if c not in cats:
                cats.append(c)

    pois: list[tuple[Poi, str]] = []
    for c in cats[:5]:
        try:
            pois += [(p, c) for p in fly.search_pois(intent.city, category=c)[:4]]
        except FlyaiError as e:
            warnings.append(str(e))

    out, seen = [], set()
    for poi, cat in pois:
        if poi.name in seen:
            continue
        seen.add(poi.name)
        lat, lng = poi.lat, poi.lng
        if (lat is None or lng is None) and geo.enabled:
            loc = geo.geocode(poi.address or poi.name, intent.city)
            lat, lng = loc if loc else (None, None)
        dist = None
        if None not in (lat, lng, hotel.lat, hotel.lng):
            dist = haversine_m(hotel.lat, hotel.lng, lat, lng)
        ring, minutes = ring_for(dist)
        exp = Experience(
            id=f"p{len(out)}", title=poi.name[:16],
            detail=f"{cat}" + (f"｜门票 {poi.ticket_price}" if poi.ticket_price else ""),
            source="周边数据", evidence=poi.address, ring=ring, distance_m=dist, minutes=minutes,
            url=poi.url, personas=[p for p in personas if cat in PERSONA_POI[p]],
        )
        if dist is not None:
            exp.bearing = round(bearing_deg(hotel.lat, hotel.lng, lat, lng))
        out.append(exp)
    # 空间优先：近的在前，距离未知的放最后；超过 50km 的不算 staycation 周边
    out = [e for e in out if e.distance_m is None or e.distance_m <= 50000]
    out.sort(key=lambda e: (e.distance_m is None, e.distance_m or 0))
    return out[:8]


# ---------------------------------------------------------------- 4. 旅程编排
JOURNEY_SYSTEM = """你是高品质酒店的私人旅程设计师。根据住客人群，从给定体验清单中挑选并排成一段完整旅程。
硬性规则：
1. 只能使用清单中的 experience_id，不得编造新体验；
2. day=0 为入住日（{check_in} 后），day=1 为次日（{check_out} 前退房；退房后可安排 1 项“车程圈/步行圈”体验作为收尾，day=1 时间可晚于退房）；
3. 车程圈体验要留出路上时间；
4. 生成 3-6 条主动提醒（不是行程复述，而是“该做什么了”：提前预约、错峰、天气/穿着、退房前别忘了），
   offset_minutes 为相对入住日 00:00 的分钟数（入住前 2 天 20:00 = -2*1440+1200）。
输出 JSON：{{"headline": 一句话概括这段旅程(≤20字),
 "timeline": [{{"day":0,"time":"HH:MM","experience_id":"..","note":"为什么这样安排(≤25字)"}}],
 "reminders": [{{"when":"入住前2天 20:00","offset_minutes":-1680,"message":"..","experience_id":"..或null"}}]}}"""


def _hm(s: str) -> int | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s).strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def checkout_time(pkgs: list[Package]) -> str:
    text = " ".join(p.name + p.selling_point for p in pkgs)
    m = re.search(r"延(?:迟|时)?退(?:房)?(?:至)?\s*(\d{1,2})(?::00|点|时)", text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return "14:00" if re.search(r"延迟退房|延时退房|延退", text) else DEFAULT_CHECK_OUT


def validate_journey(raw: dict, persona: str, exps: dict[str, Experience],
                     check_out: str) -> Journey | None:
    ci, co = _hm(CHECK_IN), _hm(check_out)
    items, used = [], set()
    for it in raw.get("timeline") or []:
        eid, t = it.get("experience_id"), _hm(it.get("time", ""))
        day = it.get("day")
        if eid not in exps or t is None or day not in (0, 1) or eid in used:
            continue
        if day == 0 and t < ci:
            continue
        # 次日：酒店内体验必须在退房前；周边体验可作为退房后收尾（不晚于 18:00）
        if day == 1 and ((exps[eid].ring == "酒店内" and t >= co) or t > 18 * 60):
            continue
        used.add(eid)
        items.append(TimelineItem(day=day, time=f"{t // 60:02d}:{t % 60:02d}", experience_id=eid,
                                  title=exps[eid].title, note=str(it.get("note") or "")[:40]))
    if len(items) < 2:
        return None
    items.sort(key=lambda x: (x.day, x.time))

    reminders = []
    for r in raw.get("reminders") or []:
        try:
            off = int(r.get("offset_minutes"))
        except (TypeError, ValueError):
            continue
        eid = r.get("experience_id")
        if eid is not None and eid not in exps:
            eid = None
        if not r.get("message") or off > 2 * 1440:
            continue
        reminders.append(Reminder(when=str(r.get("when") or ""), offset_minutes=off,
                                  message=str(r["message"])[:80], experience_id=eid))
    reminders.sort(key=lambda r: r.offset_minutes)
    return Journey(persona=persona, headline=str(raw.get("headline") or f"{persona}的一晚")[:24],
                   timeline=items, reminders=reminders[:6])


def fallback_journey(persona: str, exps: list[Experience], check_out: str) -> Journey:
    """模型输出不合格时的确定性兜底：保证核心路径总能出结果。"""
    fit = sorted(exps, key=lambda e: (persona not in e.personas, e.ring != "酒店内", e.distance_m or 0))
    inside = [e for e in fit if e.ring == "酒店内"]
    outside = [e for e in fit if e.ring != "酒店内"]
    slots = [(0, "15:00"), (0, "17:30"), (0, "20:00"), (1, "08:00"), (1, "10:00")]
    items = [TimelineItem(day=d, time=t, experience_id=e.id, title=e.title, note="")
             for (d, t), e in zip(slots, inside)]
    if outside:
        e = outside[0]
        items.append(TimelineItem(day=1, time=f"{_hm(check_out) // 60 + 1:02d}:00",
                                   experience_id=e.id, title=e.title, note="退房后顺路收尾"))
    reminders = [Reminder(when="入住前 1 天 20:00", offset_minutes=-240,
                          message="确认入住人数与到店时间，需要预约的项目今晚约好")]
    return Journey(persona=persona, headline=f"{persona}的一晚", timeline=items, reminders=reminders)


def build_journey(llm: LLM, intent: Intent, persona: str, hotel: Hotel,
                  exps: list[Experience], check_out: str) -> Journey:
    listing = [
        {"experience_id": e.id, "title": e.title, "detail": e.detail, "source": e.source,
         "ring": e.ring, "minutes": e.minutes, "personas": e.personas}
        for e in exps
    ]
    user = (f"酒店：{hotel.name}（{hotel.star}）\n入住 {intent.check_in} {CHECK_IN}，退房 {intent.check_out} {check_out}\n"
            f"住客人群：{persona}；想要：{'、'.join(intent.wants) or '未说明'}\n"
            f"体验清单：{json.dumps(listing, ensure_ascii=False)}")
    by_id = {e.id: e for e in exps}
    try:
        raw = llm.chat_json(JOURNEY_SYSTEM.format(check_in=CHECK_IN, check_out=check_out), user)
        j = validate_journey(raw, persona, by_id, check_out)
        if j:
            return j
    except (LLMError, json.JSONDecodeError):
        pass
    return fallback_journey(persona, exps, check_out)


# ---------------------------------------------------------------- 5. 待确认清单
def to_confirm_list(hotel: Hotel, exps: list[Experience]) -> list[ToConfirm]:
    out = []
    for e in exps:
        if e.source == "图片识别":
            out.append(ToConfirm(
                experience_id=e.id, question=f"「{e.title}」是否对住客开放？开放时间和是否收费？",
                script=f"您好，想咨询一下：入住期间{e.title}是否对住客开放？几点到几点？需要额外付费或预约吗？谢谢！",
            ))
        elif e.source == "套餐权益" and re.search(r"下午茶|SPA|晚餐|水疗|乐园|课程|体验", e.title + e.evidence):
            out.append(ToConfirm(
                experience_id=e.id, question=f"「{e.title}」需要提前预约吗？可用时段？",
                script=f"您好，我订了含「{e.title}」的套餐，想提前预约一下，请问有哪些时段可选？",
            ))
    return out[:6]


# ---------------------------------------------------------------- 总流程
def plan(llm: LLM, fly: FlyaiClient, geo: Geocoder, text: str,
         today: dt.date | None = None, intent: Intent | None = None) -> PlanResponse:
    today = today or dt.date.today()
    warnings: list[str] = []
    intent = intent or parse_intent(llm, text, today)

    hotels, packages = recall(fly, intent, warnings)
    if not hotels:
        raise FlyaiError(f"没有找到 {intent.city} {intent.check_in} 的可订酒店：" + "；".join(warnings))
    ranked = rank_hotels(hotels, packages, intent)
    hotel = ranked[0]
    pkgs = packages_of(hotel, packages)
    if not pkgs:  # 城市级套餐多为集团通兑，再按选中酒店精确查一次
        try:
            pkgs = packages_of(hotel, fly.search_packages(intent.city, hotel_name=hotel.name))
        except FlyaiError as e:
            warnings.append(str(e))

    contrast = CONTRAST[intent.persona]
    personas = [intent.persona, contrast]

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_text = pool.submit(mine_hotel_experiences, llm, hotel, pkgs)
        f_near = pool.submit(nearby_experiences, fly, geo, hotel, intent, personas, warnings)
        images = [u for u in [hotel.pic] + [p.pic for p in pkgs] if u][:3]
        text_exps = f_text.result()
        f_img = pool.submit(mine_image_experiences, llm, images, {e.title for e in text_exps})
        near_exps = f_near.result()
        img_exps = f_img.result()

    exps = text_exps + img_exps + near_exps
    if not text_exps and not img_exps:
        warnings.append("该酒店暂无套餐与评价可供挖掘，旅程以周边体验为主")
    co = checkout_time(pkgs)

    with ThreadPoolExecutor(max_workers=2) as pool:
        journeys = list(pool.map(lambda p: build_journey(llm, intent, p, hotel, exps, co), personas))

    return PlanResponse(
        intent=intent, hotel=hotel, alternatives=ranked[1:4], packages=pkgs, experiences=exps,
        journeys=journeys, to_confirm=to_confirm_list(hotel, exps), data_mode=fly.mode,
        warnings=warnings,
    )


SCREENSHOT_PROMPT = """这是一张旅行种草截图（小红书/抖音等）。识别其中提到的酒店。
输出 JSON：{"hotel_name": 酒店名或 null, "city": 城市或 null, "highlights": [截图中提到的亮点，≤5条]}"""


def read_screenshot(llm: LLM, image_data_url: str) -> dict:
    raw = llm.vision_json(image_data_url, SCREENSHOT_PROMPT)
    return {"hotel_name": raw.get("hotel_name"), "city": raw.get("city"),
            "highlights": [str(h) for h in (raw.get("highlights") or [])][:5]}
