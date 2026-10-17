import datetime as dt

from test_route import POIS, US, US_FLY, FakeFly, FakeLLM, TEXT, TODAY, fake_geo, poi

from app.alternatives import suggest
from app.reminders import build_reminders, to_ics
from app.route import plan_route
from app.travelmate import assess

D = dt.datetime


def planned(reply=None, fly=None, text=TEXT):
    geo = fake_geo()
    a = assess(FakeLLM({**US, **(reply or {})}), geo, text, TODAY)
    return a, plan_route(a, fly or FakeFly(US_FLY), geo), geo


# ---- 主动提醒 ----

def test_reminders_for_us_trip():
    a, it, _ = planned()
    rs = build_reminders(a, it)
    kinds = [r.kind for r in rs]
    assert kinds[:3] == ["before_trip", "arrival", "clock"]
    assert rs == sorted(rs, key=lambda r: r.at)
    assert rs[2].at == D(2026, 10, 27, 0, 0)  # 入境次日 0 时
    exit_at = it.exit_leg.dep_time
    assert {r.at for r in rs if r.kind == "exit"} == {exit_at - dt.timedelta(hours=h) for h in (48, 24, 6)}
    assert any(r.kind == "area" and "Jiuzhaigou" in r.title for r in rs)  # 到成都时提醒别顺路去九寨沟
    assert all(r.title and r.body for r in rs)


def test_leg_reminders_name_station_and_time():
    a, it, _ = planned()
    rs = [r for r in build_reminders(a, it) if r.kind == "leg"]
    g245 = [r for r in rs if "G245" in r.title or "G245" in r.body]
    assert g245[0].at == D(2026, 10, 28, 20, 0) and "08:00" in g245[0].body
    leave = [r for r in rs if r.title.startswith("Leave now")][0]
    assert leave.at == D(2026, 10, 29, 6, 0)  # 08:00 的车：提前 60 分钟到站，再留 1 小时路程
    assert leave.zh == f"请送我去 {it.legs[0].dep_station}"  # 给司机看的中文


def test_no_reminders_before_landing_except_pre_trip():
    a, it, _ = planned({"entry_time": "20:00"}, text="landing 8pm")
    rs = build_reminders(a, it)
    assert all(r.kind == "before_trip" or r.at >= a.request.entry_time for r in rs)


def test_deadline_reminder_skipped_when_leaving_early():
    a, it, _ = planned()  # 11-04 10:55 离境，截止 11-06 00:00
    assert not any(r.kind == "deadline" for r in build_reminders(a, it))
    a, it, _ = planned({"exit_date": "2026-11-05"}, FakeFly({**US_FLY, ("航班", "成都", "首尔"): []}))
    assert any(r.kind == "deadline" for r in build_reminders(a, it))  # 没订到航班：最后 6 小时要提醒


def test_unilateral_traveler_gets_no_transit_specific_reminders():
    a, it, _ = planned({"nationality": "德国"})
    kinds = {r.kind for r in build_reminders(a, it)}
    assert "before_trip" not in kinds and "arrival" not in kinds and "clock" in kinds


def test_no_reminders_without_verdict():
    a, it, _ = planned({"nationality": None})
    assert build_reminders(a, it) == []


def test_ics_is_valid_calendar():
    a, it, _ = planned()
    ics = to_ics(build_reminders(a, it))
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert ics.count("BEGIN:VEVENT") == ics.count("BEGIN:VALARM") == len(build_reminders(a, it))
    assert "DTSTART;TZID=Asia/Shanghai:20261027T000000" in ics
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert "\\," in ics  # 逗号已转义


# ---- 出界替代推荐 ----

ALT_POIS = {
    "青城山": [poi("青城山", "四川省", "成都市", 103.57, 30.90)],
    "峨眉山": [poi("峨眉山", "四川省", "乐山市", 103.33, 29.52)],
    "黄龙风景区": [poi("黄龙风景区", "四川省", "阿坝藏族羌族自治州", 103.82, 32.75)],
    "西湖": [poi("西湖", "浙江省", "杭州市", 120.15, 30.25)],
    "都江堰": [poi("都江堰景区", "四川省", "成都市", 103.62, 31.00)],
    "布达拉宫": [poi("布达拉宫", "西藏自治区", "拉萨市", 91.12, 29.65)],
}


class AltLLM:
    def __init__(self, items):
        self.items = items
        self.user = ""

    def chat_json(self, system, user):
        self.user = user
        return {"items": self.items}


def alt(items):
    a, it, geo = planned()
    POIS.update(ALT_POIS)
    llm = AltLLM(items)
    return suggest(llm, geo, a), llm


def test_alternatives_validated_by_rules():
    res, llm = alt([
        {"for": "九寨沟", "zh": "黄龙风景区", "en": "Huanglong", "why": "same"},  # 同在阿坝州：拦下
        {"for": "九寨沟", "zh": "青城山", "city": "成都", "en": "Mount Qingcheng", "why": "forest"},
        {"for": "九寨沟", "zh": "布达拉宫", "en": "Potala", "why": "x"},  # 西藏：拦下
        {"for": "九寨沟", "zh": "某不存在的景点", "en": "?", "why": "x"},  # 定位不到：拦下
        {"for": "九寨沟", "zh": "峨眉山", "en": "Mount Emei", "why": "mountains"},
    ])
    assert [x.zh for x in res.items] == ["青城山", "峨眉山"]
    assert set(res.rejected) == {"黄龙风景区", "布达拉宫", "某不存在的景点"}
    q = res.items[0]
    assert q.near_city == "成都" and q.near_city_en == "Chengdu" and 0 < q.km < 100
    assert "阿坝" in llm.user  # 告诉模型原景点所在的州


def test_alternatives_capped_and_only_for_skipped():
    res, _ = alt([{"for": "九寨沟", "zh": z, "en": z, "why": ""} for z in ("青城山", "峨眉山", "都江堰", "西湖")]
                 + [{"for": "西湖", "zh": "青城山", "en": "x", "why": ""}, "garbage"])
    assert [x.zh for x in res.items] == ["青城山", "峨眉山", "都江堰"]


def test_alternatives_far_from_route_rejected():
    """西湖离成都 1600 公里：虽在允许区域，也不推荐给要去九寨沟的人。"""
    a, it, geo = planned({"places": [{"text": "pandas", "zh": "成都大熊猫繁育研究基地"},
                                     {"text": "Jiuzhaigou", "zh": "九寨沟"}], "entry_port": "成都双流国际机场",
                          "exit_port": "成都双流国际机场"})
    POIS.update(ALT_POIS)
    res = suggest(AltLLM([{"for": "九寨沟", "zh": "西湖", "en": "West Lake", "why": ""}]), geo, a)
    assert res.items == [] and res.rejected == ["西湖"]


def test_no_llm_call_when_nothing_skipped():
    a, it, geo = planned({"places": [{"text": "Hangzhou", "zh": "杭州"}]})
    llm = AltLLM([])
    assert suggest(llm, geo, a).items == [] and llm.user == ""
