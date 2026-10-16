import datetime as dt

import httpx
import pytest

from app.geo import Geocoder
from app.travelmate import PlaceAsk, assess, locate, parse_request

TODAY = dt.date(2026, 10, 17)

US_TRIP = {
    "nationality": "美国", "origin": "美国", "destination": "韩国",
    "entry_port": "上海浦东国际机场", "entry_city": "上海", "entry_date": "2026-10-26", "entry_time": "14:30",
    "exit_port": None, "exit_city": None, "exit_date": "2026-11-04", "exit_time": None,
    "places": [{"text": "Hangzhou", "zh": "杭州", "city": None},
               {"text": "the pandas in Chengdu", "zh": "成都大熊猫繁育研究基地", "city": "成都"},
               {"text": "Jiuzhaigou", "zh": "九寨沟", "city": None}],
    "purpose": "旅游",
}


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.system = ""

    def chat_json(self, system, user):
        self.system = system
        return self.reply


def trip(**kw):
    return FakeLLM({**US_TRIP, **kw})


# 高德返回：按关键词给 POI；"香格里拉"第一条是同名酒店；"某小众民宿"搜不到
POIS = {
    "杭州": [{"name": "杭州市", "type": "地名地址信息;普通地名;地市级地名", "pname": "浙江省", "cityname": "杭州市",
             "adname": [], "location": "120.15,30.28"}],
    "成都大熊猫繁育研究基地": [{"name": "成都大熊猫繁育研究基地", "type": "风景名胜;风景名胜;国家级景点",
                         "pname": "四川省", "cityname": "成都市", "adname": "成华区", "location": "104.14,30.73"}],
    "九寨沟": [{"name": "九寨沟", "type": "风景名胜;风景名胜;国家级景点", "pname": "四川省",
               "cityname": "阿坝藏族羌族自治州", "adname": "九寨沟县", "location": "103.92,33.26"}],
    "香格里拉": [{"name": "北京香格里拉饭店", "type": "住宿服务;宾馆酒店;五星级宾馆", "pname": "北京市",
                "cityname": "北京市", "adname": "海淀区", "location": "116.3,39.9"},
               {"name": "香格里拉市", "type": "地名地址信息;普通地名;区县级地名", "pname": "云南省",
                "cityname": "迪庆藏族自治州", "adname": "香格里拉市", "location": "99.7,27.8"}],
}
GEOCODES = {"梅里雪山": [{"province": "云南省", "city": "迪庆藏族自治州", "district": "德钦县", "location": "98.6,28.4"}]}


def fake_geo(calls=None):
    def handler(request: httpx.Request):
        kw = request.url.params.get("keywords") or request.url.params.get("address")
        if calls is not None:
            calls.append((request.url.path, kw))
        if request.url.path.endswith("/place/text"):
            return httpx.Response(200, json={"pois": POIS.get(kw, [])})
        return httpx.Response(200, json={"geocodes": GEOCODES.get(kw, [])})
    return Geocoder(key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))


# ---- 需求解析 ----

def test_parse_full_request():
    r = parse_request(trip(), "…", TODAY)
    assert (r.nationality, r.origin, r.destination) == ("美国", "美国", "韩国")
    assert r.entry_time == dt.datetime(2026, 10, 26, 14, 30)
    assert r.exit_time == dt.datetime(2026, 11, 4, 12, 0)
    assert r.assumptions == ["离境时刻未说明，暂按 11-04 12:00 计算"]
    assert [p.zh for p in r.places] == ["杭州", "成都大熊猫繁育研究基地", "九寨沟"]
    assert r.missing == []


def test_prompt_lists_country_names():
    llm = trip()
    parse_request(llm, "…", TODAY)
    assert "吉尔吉斯斯坦" in llm.system and "秘鲁" in llm.system


def test_country_aliases():
    r = parse_request(trip(nationality="美利坚合众国", destination="中国香港"), "…", TODAY)
    assert (r.nationality, r.destination) == ("美国", "香港")


def test_past_date_rolls_to_next_year():
    r = parse_request(trip(entry_date="2025-10-26", exit_date="2025-11-04"), "…", TODAY)
    assert r.entry_time.year == 2026 and r.exit_time.year == 2026


@pytest.mark.parametrize("v", [None, "null", "", "unknown"])
def test_null_like_values(v):
    r = parse_request(trip(destination=v), "…", TODAY)
    assert r.destination is None and "离开中国后去哪个国家" in r.missing


def test_origin_defaults_to_nationality():
    r = parse_request(trip(origin=None), "…", TODAY)
    assert r.origin == "美国" and any("按国籍国" in a for a in r.assumptions)


def test_missing_fields_for_transit_country():
    r = parse_request(trip(entry_port=None, entry_city=None, entry_date=None), "…", TODAY)
    assert r.missing == ["入境日期", "入境机场"]


def test_unilateral_country_needs_no_port_or_destination():
    r = parse_request(trip(nationality="德国", entry_port=None, entry_city=None, destination=None), "…", TODAY)
    assert r.missing == []


def test_unknown_nationality_is_the_only_question():
    r = parse_request(trip(nationality=None, entry_port=None, entry_city=None), "…", TODAY)
    assert r.missing == ["国籍"]


def test_city_with_one_port_fills_it():
    r = parse_request(trip(entry_port=None, entry_city="昆明"), "…", TODAY)
    assert r.entry_port == "昆明长水国际机场" and any("只有一个适用口岸" in a for a in r.assumptions)


def test_city_with_several_ports_asks_which():
    r = parse_request(trip(entry_port=None, entry_city="成都"), "…", TODAY)
    assert r.entry_port is None
    assert r.missing == ["入境机场（成都有 2 个：成都双流国际机场、成都天府国际机场）"]


def test_ambiguous_exit_city_does_not_block():
    r = parse_request(trip(exit_city="上海"), "…", TODAY)
    assert r.missing == [] and r.exit_port is None
    assert any("上海有 3 个" in a and "暂不校验" in a for a in r.assumptions)


def test_places_dedupe_and_bad_items():
    r = parse_request(trip(places=[{"text": "HZ", "zh": "杭州"}, {"text": "Hangzhou", "zh": "杭州"},
                                   "garbage", {"text": "x", "zh": None}]), "…", TODAY)
    assert [(p.text, p.zh) for p in r.places] == [("HZ", "杭州")]


def test_unknown_purpose_defaults_to_tourism():
    assert parse_request(trip(purpose="sightseeing"), "…", TODAY).purpose == "旅游"


# ---- 地名定位 ----

def test_locate_skips_same_name_hotel():
    [s] = locate(fake_geo(), [PlaceAsk(text="Shangri-La", zh="香格里拉")])
    assert (s.place.province, s.place.city) == ("云南省", "迪庆藏族自治州")


def test_locate_falls_back_to_geocode():
    [s] = locate(fake_geo(), [PlaceAsk(text="Meili Snow Mountain", zh="梅里雪山")])
    assert s.place.city == "迪庆藏族自治州" and s.place.source == "高德地理编码"


def test_locate_handles_amap_empty_list_fields():
    [s] = locate(fake_geo(), [PlaceAsk(text="Hangzhou", zh="杭州")])
    assert s.place.district == "" and s.place.lat == 30.28


def test_locate_unknown_and_memo():
    calls = []
    geo = fake_geo(calls)
    locate(geo, [PlaceAsk(text="x", zh="某小众民宿")])
    locate(geo, [PlaceAsk(text="x", zh="某小众民宿")])
    assert len(calls) == 2  # 地点搜索 + 地理编码各一次，第二次走缓存


def test_locate_without_key():
    [s] = locate(Geocoder(key=""), [PlaceAsk(text="Hangzhou", zh="杭州")])
    assert s.place is None


# ---- 端到端评估 ----

def test_assess_blocks_jiuzhaigou():
    a = assess(trip(), fake_geo(), "…", TODAY)
    v = a.verdict
    assert not v.ok and v.deadline == dt.datetime(2026, 11, 6, 0, 0)
    assert [(i.rule, i.level, i.subject) for i in v.issues] == [("R6", "block", "九寨沟")]
    assert a.rules_as_of == "2026-10-16"


def test_assess_unlocated_place_is_warn_not_block():
    a = assess(trip(places=[{"text": "a tiny guesthouse", "zh": "某小众民宿"}]), fake_geo(), "…", TODAY)
    assert a.verdict.ok
    assert [(i.rule, i.level) for i in a.verdict.issues] == [("R6", "warn")]


def test_assess_stops_when_info_missing():
    a = assess(trip(nationality=None), fake_geo(), "…", TODAY)
    assert a.verdict is None and a.request.missing == ["国籍"]
    assert len(a.stops) == 3  # 地点照样定位，页面可以先画出来


def test_assess_german_to_shangri_la_ok():
    a = assess(trip(nationality="德国", origin="泰国", places=[{"text": "Shangri-La", "zh": "香格里拉"}]),
               fake_geo(), "…", TODAY)
    assert a.verdict.ok and a.verdict.policy == "unilateral_30d"
