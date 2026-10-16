import datetime as dt

import httpx

from app.flyai import FlyaiError
from app.geo import Geocoder
from app.route import CityPlan, _allocate_nights, pick, plan_route, short_city
from app.schemas import Leg
from app.travelmate import assess

TODAY = dt.date(2026, 10, 17)
D = dt.datetime


def leg(no, dep, arr, mode="火车", code="", transfers=0, price=100.0, dep_station="站"):
    return Leg(mode=mode, no=no, dep_station=dep_station, dep_code=code, arr_station="站", dep_time=dep, arr_time=arr,
               minutes=int((arr - dep).total_seconds() // 60), transfers=transfers, price=price)


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def chat_json(self, system, user):
        return self.reply


US = {
    "nationality": "美国", "origin": "美国", "destination": "韩国", "destination_city": "首尔",
    "entry_port": "上海浦东国际机场", "entry_date": "2026-10-26", "entry_time": "14:30",
    "exit_port": None, "exit_city": None, "exit_date": "2026-11-04", "exit_time": None,
    "places": [{"text": "Shanghai", "zh": "上海"}, {"text": "Hangzhou", "zh": "杭州"},
               {"text": "pandas", "zh": "成都大熊猫繁育研究基地", "city": "成都"},
               {"text": "Jiuzhaigou", "zh": "九寨沟"}],
}
TEXT = "landing at 2:30pm"


def poi(name, pname, city, lng, lat, type_="风景名胜"):
    return {"name": name, "type": type_, "pname": pname, "cityname": city, "adname": "", "location": f"{lng},{lat}"}


POIS = {
    "上海": [poi("上海市", "上海市", "上海市", 121.47, 31.23, "地名地址信息")],
    "杭州": [poi("杭州市", "浙江省", "杭州市", 120.15, 30.28, "地名地址信息")],
    "成都大熊猫繁育研究基地": [poi("成都大熊猫繁育研究基地", "四川省", "成都市", 104.14, 30.73)],
    "九寨沟": [poi("九寨沟", "四川省", "阿坝藏族羌族自治州", 103.92, 33.26)],
    "乐山大佛": [poi("乐山大佛", "四川省", "乐山市", 103.77, 29.54)],
    "长城": [poi("八达岭长城", "北京市", "北京市", 116.0, 40.36)],
    "兵马俑": [poi("秦始皇帝陵博物院", "陕西省", "西安市", 109.27, 34.38)],
    "上海浦东国际机场": [poi("上海浦东国际机场", "上海市", "上海市", 121.8, 31.15, "交通设施服务")],
    "北京首都国际机场": [poi("北京首都国际机场", "北京市", "北京市", 116.6, 40.08, "交通设施服务")],
    "西安咸阳国际机场": [poi("西安咸阳国际机场", "陕西省", "咸阳市", 108.76, 34.44, "交通设施服务")],
    "成都双流国际机场": [poi("成都双流国际机场", "四川省", "成都市", 103.95, 30.57, "交通设施服务")],
}


def fake_geo():
    def handler(request: httpx.Request):
        kw = request.url.params.get("keywords")
        if request.url.path.endswith("/place/text"):
            return httpx.Response(200, json={"pois": POIS.get(kw, [])})
        return httpx.Response(200, json={"geocodes": []})
    return Geocoder(key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))


class FakeFly:
    """按 (方式, 出发, 到达) 返回预置结果；记下每次调用。"""

    def __init__(self, data=None, fail=()):
        self.data = data or {}
        self.fail = set(fail)
        self.calls = []

    def _get(self, mode, a, b, date, after_hour):
        self.calls.append((mode, a, b, date, after_hour))
        if (mode, a, b) in self.fail:
            raise FlyaiError("风控")
        return self.data.get((mode, a, b), [])

    def search_trains(self, a, b, date, after_hour=None):
        return self._get("火车", a, b, date, after_hour)

    def search_flights(self, a, b, date, after_hour=None):
        return self._get("航班", a, b, date, after_hour)


US_FLY = {
    ("火车", "上海", "杭州"): [leg("G7331", D(2026, 10, 29, 6, 13), D(2026, 10, 29, 7, 7)),
                           leg("G245", D(2026, 10, 29, 8, 0), D(2026, 10, 29, 8, 45))],
    ("火车", "杭州", "成都"): [leg("G237", D(2026, 11, 1, 9, 53), D(2026, 11, 1, 19, 35))],
    ("航班", "杭州", "成都"): [leg("3U8918", D(2026, 11, 1, 9, 15), D(2026, 11, 1, 12, 25), "航班", "HGH"),
                           leg("3U6932", D(2026, 11, 1, 19, 55), D(2026, 11, 1, 22, 55), "航班", "HGH")],
    ("航班", "成都", "首尔"): [leg("XX1", D(2026, 11, 4, 11, 0), D(2026, 11, 4, 16, 0), "航班", "ZZZ"),  # 非适用口岸
                           leg("SC8820", D(2026, 11, 4, 10, 55), D(2026, 11, 4, 18, 0), "航班", "TFU", 1),
                           leg("3U3961", D(2026, 11, 4, 13, 30), D(2026, 11, 4, 19, 0), "航班", "CTU")],
}


def plan(reply=None, fly=None, text=TEXT):
    geo = fake_geo()
    a = assess(FakeLLM({**US, **(reply or {})}), geo, text, TODAY)
    return plan_route(a, fly or FakeFly(US_FLY), geo)


# ---- 完整行程 ----

def test_full_us_itinerary():
    it = plan()
    assert it.skipped == ["九寨沟"]
    assert [c.name for c in it.cities] == ["上海", "杭州", "成都"]
    assert [c.name_en for c in it.cities] == ["Shanghai", "Hangzhou", "Chengdu"]
    assert [c.arrive for c in it.cities] == [dt.date(2026, 10, 26), dt.date(2026, 10, 29), dt.date(2026, 11, 1)]
    assert sum(c.nights for c in it.cities) == 9  # 10-26 → 11-04
    assert [x.no for x in it.legs] == ["G245", "3U8918"]  # 08:00 前的车不要；9 小时高铁输给 12:25 到的航班
    assert it.exit_leg.no == "3U3961"  # 非口岸 ZZZ 排除；直达优先于中转的 SC8820
    assert it.verdict.ok and it.verdict.minutes_to_spare > 0
    assert it.flyai_calls == 4


def test_short_train_skips_flight_search():
    fly = FakeFly(US_FLY)
    plan(fly=fly)
    assert ("航班", "上海", "杭州") not in [c[:3] for c in fly.calls]
    assert ("航班", "杭州", "成都") in [c[:3] for c in fly.calls]


def test_queries_pass_departure_hour():
    """flyai 只返回 10 条且按时间排，不带出发时段会全是清晨车次。"""
    fly = FakeFly(US_FLY)
    plan(fly=fly)
    assert all(c[4] is not None and c[4] >= 8 for c in fly.calls)


def test_first_leg_on_arrival_day_waits_for_landing():
    fly = FakeFly({("火车", "上海", "杭州"): [leg("G1", D(2026, 10, 26, 15, 0), D(2026, 10, 26, 16, 0)),
                                             leg("G2", D(2026, 10, 26, 17, 0), D(2026, 10, 26, 18, 0))]})
    it = plan({"places": [{"text": "Hangzhou", "zh": "杭州"}], "exit_date": None, "destination_city": None}, fly)
    assert it.cities[0].name == "上海" and it.cities[0].nights == 0  # 只是落地城市，当天就走
    assert it.legs[0].no == "G2"  # 14:30 落地 + 2 小时，15:00 的车赶不上


def test_booked_exit_flight_is_not_searched():
    fly = FakeFly(US_FLY)
    it = plan({"exit_time": "10:30", "exit_port": "成都双流国际机场"}, fly, text="land 2:30pm, flight out 10:30")
    assert it.exit_leg_booked and it.exit_leg is None
    assert ("航班", "成都", "首尔") not in [c[:3] for c in fly.calls]
    assert it.verdict.ok


def test_exit_date_past_deadline_moves_to_last_day():
    fly = FakeFly({**US_FLY, ("航班", "成都", "首尔"): [
        leg("3U1", D(2026, 11, 5, 13, 30), D(2026, 11, 5, 19, 0), "航班", "CTU")]})
    it = plan({"exit_date": "2026-11-08"}, fly)  # 截止 11-06 00:00，最后可离境日 11-05
    assert any(i.rule == "R5" and i.level == "info" and "11-05" in i.message for i in it.issues)
    assert it.exit_leg.no == "3U1" and it.verdict.ok


def test_exit_flight_must_leave_before_deadline():
    fly = FakeFly({**US_FLY, ("航班", "成都", "首尔"): [
        leg("LATE", D(2026, 11, 6, 0, 30), D(2026, 11, 6, 5, 0), "航班", "CTU")]})
    it = plan({"exit_date": "2026-11-05"}, fly)
    assert it.exit_leg is None
    assert any(i.rule == "交通" for i in it.issues)


def test_airport_in_neighbor_city_counts_as_that_city():
    """西安咸阳机场在咸阳，但离境城市应是西安，不能多排一段西安→咸阳。"""
    it = plan({"entry_port": "北京首都国际机场", "exit_port": "西安咸阳国际机场", "destination_city": "东京",
               "places": [{"text": "Great Wall", "zh": "长城"}, {"text": "Terracotta", "zh": "兵马俑"}]},
              FakeFly({}))
    assert [c.name for c in it.cities] == ["北京", "西安"]


def test_exit_city_without_airport_goes_back_to_one():
    it = plan({"places": [{"text": "pandas", "zh": "成都大熊猫繁育研究基地"}, {"text": "Leshan", "zh": "乐山大佛"}],
               "exit_date": None}, FakeFly({}))
    assert [c.name for c in it.cities] == ["上海", "成都", "乐山", "成都"]
    assert it.cities[-1].nights == 0


def test_flyai_failure_becomes_warning():
    it = plan(fly=FakeFly(US_FLY, fail={("火车", "上海", "杭州"), ("航班", "上海", "杭州")}))
    assert it.legs[0] is None
    assert any(i.rule == "交通" and i.subject == "上海→杭州" for i in it.issues)


def test_nothing_planned_without_verdict():
    assert plan({"nationality": None}).cities == []
    assert plan({"nationality": "印度"}).cities == []


# ---- 小函数 ----

def test_pick_prefers_direct_then_earliest_arrival():
    legs = [leg("T", D(2026, 1, 1, 8, 0), D(2026, 1, 1, 10, 0), transfers=1),
            leg("A", D(2026, 1, 1, 9, 0), D(2026, 1, 1, 13, 0)),
            leg("B", D(2026, 1, 1, 10, 0), D(2026, 1, 1, 12, 0)),
            leg("EARLY", D(2026, 1, 1, 6, 0), D(2026, 1, 1, 7, 0))]
    assert pick(legs, D(2026, 1, 1, 8, 0)).no == "B"
    assert pick(legs, D(2026, 1, 1, 8, 0), latest=D(2026, 1, 1, 9, 30)).no == "A"
    assert pick(legs, D(2026, 1, 1, 23, 0)) is None


def cities(*places):
    return [CityPlan(name=f"C{i}", province="p", city=f"C{i}", places=list(p)) for i, p in enumerate(places)]


def test_allocate_by_place_count():
    cs = cities(["a"], ["b", "c", "d"], [])
    _allocate_nights(cs, 6, issues := [])
    assert [c.nights for c in cs] == [2, 4, 0] and issues == []


def test_allocate_too_few_nights_warns():
    cs = cities(["a"], ["b"], ["c"])
    _allocate_nights(cs, 2, issues := [])
    assert sum(c.nights for c in cs) == 2 and issues[0].rule == "排程"


def test_short_city():
    assert short_city("大理白族自治州") == "大理" and short_city("杭州市") == "杭州" and short_city("北京市") == "北京"


def test_every_issue_has_english():
    """页面给外国游客看：任何提示都必须有英文。"""
    fly = FakeFly(US_FLY, fail={("火车", "上海", "杭州"), ("航班", "上海", "杭州")})
    for reply in ({}, {"destination": "美国"}, {"exit_date": "2026-11-09"}, {"destination": "香港"},
                  {"purpose": "工作"}, {"entry_port": "广深港高铁西九龙站口岸", "exit_port": "上海虹桥站"},
                  {"nationality": "德国"}, {"nationality": "印度"},
                  {"places": [{"text": "a", "zh": "某小众民宿"}] + US["places"]}):
        it = plan(reply, fly)
        a_issues = it.verdict.issues if it.verdict else []
        for i in a_issues + it.issues:
            assert i.en and not any("\u4e00" <= ch <= "\u9fff" for ch in i.en.replace(i.subject, "")), (reply, i)


def test_same_port_in_and_out_returns_to_entry_city():
    it = plan({"entry_port": "上海浦东国际机场", "exit_port": "上海浦东国际机场", "destination_city": None,
               "places": [{"text": "Hangzhou", "zh": "杭州"}]}, FakeFly({}))
    assert [c.name for c in it.cities] == ["上海", "杭州", "上海"]


def test_hopeless_trip_skips_exit_flight_search():
    fly = FakeFly(US_FLY)
    it = plan({"destination": "美国", "destination_city": "洛杉矶"}, fly)
    assert all(c[0] != "航班" or c[2] != "洛杉矶" for c in fly.calls) and it.exit_leg is None


def test_flyai_error_on_exit_flight_reads_as_not_found():
    it = plan(fly=FakeFly(US_FLY, fail={("航班", "成都", "首尔")}))
    [i] = [i for i in it.issues if i.rule == "交通"]
    assert "No flight to Seoul" in i.en


def test_exit_airport_note_dropped_once_flight_chosen():
    geo = fake_geo()
    a = assess(FakeLLM({**US, "exit_city": "上海"}), geo, TEXT, TODAY)
    assert any(x.startswith("离境机场") for x in a.request.assumptions)
    it = plan_route(a, FakeFly(US_FLY), geo)
    assert it.exit_leg is not None
    assert not any(x.startswith("离境机场") for x in a.request.assumptions)
    assert len(a.request.assumptions) == len(a.request.assumptions_en)
