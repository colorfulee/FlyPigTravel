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
    if not re.search(r"[五四5４]星|豪华|高档", text):  # 模型常凭酒店品牌脑补星级，只认用户原话
        min_star = None
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
# 没说想要什么时，用人群词去飞猪检索
PERSONA_RECALL = {"亲子": "亲子", "情侣": "景观", "独处": "安静", "闺蜜": "网红", "带父母": "园林"}


def recall_keywords(intent: Intent) -> list[str]:
    """用户想要的体验直接作为飞猪检索词：“江景”能召回滨江酒店，通用列表里往往没有。"""
    words = [w for w in intent.wants if 1 < len(w) <= 6][:2]
    return words or [PERSONA_RECALL[intent.persona]]


def recall(fly: FlyaiClient, intent: Intent, warnings: list[str]) -> tuple[list[Hotel], list[Package]]:
    hotels: dict[str, Hotel] = {}
    max_price = int(intent.budget * 1.3) if intent.budget else None
    stars = "5" if intent.min_star == 5 else "4,5"
    dates = (intent.city, intent.check_in, intent.check_out)
    searches = [
        ("", lambda: fly.search_hotels(*dates, max_price=max_price, stars=stars)),
        ("", lambda: fly.search_marriott_hotels(*dates, max_price=max_price)),
    ] + [
        (w, lambda w=w: fly.search_hotels(*dates, max_price=max_price, keywords=w, stars=stars))
        for w in recall_keywords(intent)
    ]
    if intent.hotel_hint:  # 点名的酒店不一定在前几条结果里，单独按名称搜
        searches.insert(0, ("", lambda: fly.search_hotels(*dates, keywords=intent.hotel_hint, stars=None)))
    for tag, search in searches:
        try:
            for found in search():
                h = hotels.setdefault(found.name, found)
                if tag and tag not in h.recall_tags:
                    h.recall_tags.append(tag)
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
# 线上数据没有评分和评价，品牌是少数可靠的品质信号
LUXURY_BRANDS = ("JW", "丽思卡尔顿", "瑞吉", "W酒店", "艾迪逊", "宝格丽", "华尔道夫", "四季", "半岛",
                 "柏悦", "安缦", "文华东方", "康莱德", "瑰丽", "嘉佩乐", "凯宾斯基", "万豪侯爵", "璞丽")


def star_level(h: Hotel) -> int | None:
    return STAR_LEVEL.get(h.star)


def focus_categories(intent: Intent) -> list[str]:
    """本次需求真正想去的周边类型：用户点名的在前，其次是人群偏好。"""
    cats = [c for k, c in WANT_POI.items() if any(k in w for w in intent.wants)]
    return list(dict.fromkeys(cats + PERSONA_POI[intent.persona][:2]))


def location_fit(h: Hotel, pois: list[Poi]) -> tuple[float, str | None]:
    """酒店到最近 3 个想去地点的平均距离：0km 得 20 分，线性递减；超过 12km 再额外扣分，
    staycation 每次出门都要 40 分钟以上就失去了意义。"""
    if h.lat is None or h.lng is None:
        return 0.0, None
    near = sorted((haversine_m(h.lat, h.lng, p.lat, p.lng), p.name)
                  for p in pois if p.lat is not None and p.lng is not None)[:3]
    if not near:
        return 0.0, None
    avg = sum(d for d, _ in near) / len(near)
    ring, minutes = ring_for(int(avg))
    how = "步行" if ring == "步行圈" else "车程"
    km = avg / 1000
    return max(0.0, 20 - km) - max(0.0, km - 12) * 1.5, f"离「{near[0][1][:12]}」等 {len(near)} 处想去的地方平均{how}约 {minutes} 分钟"


def score_hotel(h: Hotel, packages: list[Package], intent: Intent, pois: list[Poi],
                this_year: int) -> tuple[float, list[str]]:
    s, why = (h.score or 4.5) * 10, []
    if h.score:
        why.append(f"飞猪评分 {h.score}")

    level = star_level(h)
    s += 8 if level == 5 else 0  # 高品质 staycation：同等条件优先豪华/五星
    if intent.min_star and level is not None and level < intent.min_star:
        s -= 40
    if any(b in h.name for b in LUXURY_BRANDS):
        s += 6
        why.append("奢华酒店品牌")
    if h.decorated and this_year - h.decorated <= 3:
        s += 4
        why.append(f"{h.decorated} 年装修")

    loc, loc_why = location_fit(h, pois)
    s += loc
    if loc_why:
        why.append(loc_why)

    pk = packages_of(h, packages)
    if pk:
        s += min(len(pk), 3) * 5  # 有套餐 = 有可挖掘的体验
        why.append(f"有 {len(pk)} 个套餐可挖掘体验")
    words = PERSONA_WORDS.get(intent.persona, []) + intent.wants
    text = " ".join([h.name, h.review, h.interests_poi] + [p.name + p.selling_point for p in pk]).lower()
    matched = [w for w in dict.fromkeys(words) if w and w.lower() in text]
    s += 4 * len(matched) + 6 * len(h.recall_tags)
    hits = list(dict.fromkeys(h.recall_tags + matched))
    if hits:
        why.append("符合你的需求：" + "、".join(hits[:3]))

    if intent.hotel_hint and intent.hotel_hint in h.name:
        s += 1000
        why.insert(0, "你点名的酒店")
    if intent.budget:
        if h.price is None:
            s -= 5
        elif h.price > intent.budget:
            s -= 30 * (h.price / intent.budget - 1) + 10
        else:
            s += 8 * h.price / intent.budget  # 预算内价格越接近上限，通常房型与服务越好
    elif h.price:
        s += min(h.price, 2000) / 250
    return s, why


def rank_hotels(hotels: list[Hotel], packages: list[Package], intent: Intent,
                pois: list[Poi] | None = None, today: dt.date | None = None) -> list[Hotel]:
    year = (today or dt.date.today()).year
    scored = []
    for h in hotels:
        s, h.reasons = score_hotel(h, packages, intent, pois or [], year)
        scored.append((s, h))
    return [h for _, h in sorted(scored, key=lambda x: x[0], reverse=True)]


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


def fetch_pois(fly: FlyaiClient, geo: Geocoder, intent: Intent, personas: list[str],
               warnings: list[str]) -> list[tuple[Poi, str]]:
    """查一次周边 POI，排序（选酒店）和体验编排共用，减少 flyai 调用。"""
    cats = focus_categories(intent)
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
    for poi, _ in pois:
        if (poi.lat is None or poi.lng is None) and geo.enabled:
            loc = geo.geocode(poi.address or poi.name, intent.city)
            if loc:
                poi.lat, poi.lng = loc
    return pois


def nearby_experiences(hotel: Hotel, personas: list[str],
                       pois: list[tuple[Poi, str]]) -> list[Experience]:
    out, seen = [], set()
    for poi, cat in pois:
        if poi.name in seen:
            continue
        seen.add(poi.name)
        lat, lng = poi.lat, poi.lng
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
    # 主人群至少保留 5 个名额，避免被对比人群的近处地点挤掉
    main = [e for e in out if personas[0] in e.personas][:5]
    rest = [e for e in out if e not in main][:8 - len(main)]
    return [e for e in out if e in main or e in rest]


# ---------------------------------------------------------------- 4. 旅程编排
JOURNEY_SYSTEM = """你是高品质酒店的私人旅程设计师。根据住客人群，从给定体验清单中挑选并排成一段完整旅程。
硬性规则：
1. 只能使用清单中的 experience_id，不得编造新体验；
2. day=0 为入住日（{check_in} 后），day=1 为次日（{check_out} 前退房；退房后可安排 1 项“车程圈/步行圈”体验作为收尾，day=1 时间可晚于退房）；
3. 车程圈体验要留出路上时间；
4. 生成 3-6 条主动提醒（不是行程复述，而是“该做什么了”：提前预约、错峰、天气/穿着、退房前别忘了），
   offset_minutes 为相对入住日 00:00 的分钟数（入住前 2 天 20:00 = -2*1440+1200）；
5. note 与提醒只能引用清单里给出的信息（标题、说明、依据、车程分钟数）和入住/退房时间。
   不得写出清单中没有的数字、价格、营业/开放时间、名额、优惠券、购票方式；没有依据的事项写成建议，如“到店向前台确认…”；
   每条提醒必须对应一个 experience_id 并在 message 中写出该体验的标题（退房等通用提醒由系统生成，无需输出）。
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


# 模型最爱编的“事实”：没有原文依据就出现这些词，视为编造
CLAIM_WORDS = ("优惠", "券", "限", "免费", "赠", "折", "开放", "营业", "闭馆", "售罄",
               "扫码", "购票", "需预约", "须预约", "合作", "领取", "凭", "优先", "手续", "班次",
               "寄存", "地图", "活跃", "实名")
CLAIM_RE = re.compile(r"[需须要]\S{0,3}预约")
BASIS_RE = re.compile(r"[（(]依据[:：][^）)]*[）)]")
ID_RE = re.compile(r"[（(]?(?:experience_id[:：]?\s*)?(?<![A-Za-z0-9])[hpv]\d+(?![0-9])[)）]?")


def _facts(exp: Experience | None, check_out: str) -> str:
    base = f"{CHECK_IN} {check_out}"
    if not exp:
        return base
    extra = f" {exp.minutes}" if exp.minutes is not None else ""
    return f"{base} {exp.title} {exp.detail} {exp.evidence}{extra}"


def clean(text: str) -> str:
    """去掉模型夹带的内部 id 与“（依据：…）”注释。"""
    return ID_RE.sub("", BASIS_RE.sub("", text)).strip()


def is_grounded(text: str, facts: str) -> bool:
    """文本中的数字和事实性措辞都必须能在原始数据里找到。"""
    if any(n not in facts for n in re.findall(r"\d+", text)):
        return False
    if CLAIM_RE.search(text) and not CLAIM_RE.search(facts):
        return False
    return not any(w in text and w not in facts for w in CLAIM_WORDS)


def data_reminders(items: list[TimelineItem], exps: dict[str, Experience],
                   check_out: str) -> list[Reminder]:
    """由数据直接生成的提醒，用于补足被剔除的模型提醒。"""
    out = []
    for it in items:
        e = exps[it.experience_id]
        t = it.day * 1440 + (_hm(it.time) or 0)
        if e.source == "套餐权益":
            out.append(Reminder(when=f"入住日 {CHECK_IN}", offset_minutes=_hm(CHECK_IN) or 0,
                                message=f"到店时向前台确认「{e.title}」的使用时间与方式", experience_id=e.id))
        elif e.ring == "车程圈" and e.minutes:
            out.append(Reminder(when=f"{'入住日' if it.day == 0 else '次日'} {it.time} 前",
                                offset_minutes=t - e.minutes - 15,
                                message=f"前往「{e.title}」车程约 {e.minutes} 分钟，记得提前出发", experience_id=e.id))
    co = _hm(check_out) or 12 * 60
    out.append(Reminder(when=f"次日 {check_out} 前", offset_minutes=1440 + co - 60,
                        message=f"退房时间 {check_out}，退房前检查随身物品"))
    return out


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
        note = clean(str(it.get("note") or ""))[:40]
        walk_claim = "步行" in note and exps[eid].ring not in ("酒店内", "步行圈")
        if walk_claim or not is_grounded(note, _facts(exps[eid], check_out)):
            note = ""
        items.append(TimelineItem(day=day, time=f"{t // 60:02d}:{t % 60:02d}", experience_id=eid,
                                  title=exps[eid].title, note=note))
    if len(items) < 2:
        return None
    items.sort(key=lambda x: (x.day, x.time))

    reminders = []
    for r in raw.get("reminders") or []:
        try:
            off = int(r.get("offset_minutes"))
        except (TypeError, ValueError):
            continue
        # 模型提醒必须落在某个体验上并点名它；通用提醒（退房等）只由 data_reminders 生成
        eid = r.get("experience_id")
        msg = clean(str(r.get("message") or ""))[:80]
        if eid not in exps or exps[eid].title[:4] not in msg or off > 2 * 1440:
            continue
        if not is_grounded(msg, _facts(exps[eid], check_out)):
            continue
        reminders.append(Reminder(when=str(r.get("when") or ""), offset_minutes=off,
                                  message=msg, experience_id=eid))
    covered = {r.experience_id for r in reminders}  # 模型已提醒过的体验不再重复
    reminders += [r for r in data_reminders(items, exps, check_out)
                  if r.experience_id is None or r.experience_id not in covered]
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
    if not inside:  # 没有酒店内体验时，入住日下午和晚上各安排一个周边体验
        items = [TimelineItem(day=0, time=t, experience_id=e.id, title=e.title, note="")
                 for t, e in zip(("15:30", "19:00"), outside)]
        outside = outside[len(items):]
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
        {"experience_id": e.id, "title": e.title, "detail": e.detail, "evidence": e.evidence,
         "source": e.source, "ring": e.ring, "minutes": e.minutes, "personas": e.personas}
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

    contrast = CONTRAST[intent.persona]
    personas = [intent.persona, contrast]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_pois = pool.submit(fetch_pois, fly, geo, intent, personas, warnings)
        hotels, packages = recall(fly, intent, warnings)
        pois = f_pois.result()
    if not hotels:
        raise FlyaiError(f"没有找到 {intent.city} {intent.check_in} 的可订酒店：" + "；".join(warnings))
    focus = focus_categories(intent)
    ranked = rank_hotels(hotels, packages, intent, [p for p, c in pois if c in focus], today)
    hotel = ranked[0]
    if intent.hotel_hint and intent.hotel_hint not in hotel.name:
        warnings.append(f"没有查到「{intent.hotel_hint}」在这两天的可订房，已为你推荐相近的酒店")
    pkgs = packages_of(hotel, packages)
    if not pkgs:  # 城市级套餐多为集团通兑，再按选中酒店精确查一次
        try:
            pkgs = packages_of(hotel, fly.search_packages(intent.city, hotel_name=hotel.name))
        except FlyaiError as e:
            warnings.append(str(e))

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_text = pool.submit(mine_hotel_experiences, llm, hotel, pkgs)
        f_near = pool.submit(nearby_experiences, hotel, personas, pois)
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
