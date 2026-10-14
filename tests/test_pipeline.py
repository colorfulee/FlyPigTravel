import datetime as dt

import pytest

from app.flyai import FlyaiClient, parse_price
from app.geo import Geocoder, haversine_m, ring_for
from app.pipeline import (
    checkout_time, fallback_journey, mine_hotel_experiences, next_saturday, packages_of, parse_intent, plan,
    rank_hotels, validate_journey,
)
from app.schemas import Experience, Hotel, Intent, Package

TODAY = dt.date(2026, 10, 13)  # 周二


class FakeLLM:
    """按 system prompt 路由到预置回答，模拟百炼返回。"""

    def __init__(self, journey=None, mine=None):
        self.journey = journey
        self.mine = mine
        self.calls = []

    def chat_json(self, system, user):
        self.calls.append(system[:10])
        if "需求解析器" in system:
            return {"city": "上海", "check_in": None, "check_out": None, "persona": "情侣",
                    "budget": 1500, "wants": ["夜景", "下午茶"], "hotel_hint": None}
        if "提取" in system:
            return self.mine or {"items": [
                {"title": "双人下午茶", "detail": "大堂吧享用", "source": "套餐权益",
                 "evidence": "双人下午茶", "personas": ["情侣"]},
                {"title": "江景夜景", "detail": "房间看外滩", "source": "用户评价",
                 "evidence": "江景房看外滩夜景太美了", "personas": ["情侣"]},
                {"title": "免费游艇", "detail": "编造的", "source": "套餐权益",
                 "evidence": "赠送游艇体验", "personas": []},
            ]}
        if "旅程设计师" in system:
            return self.journey or {"headline": "外滩江景的一晚", "timeline": [
                {"day": 0, "time": "15:30", "experience_id": "h0", "note": "入住先喝下午茶"},
                {"day": 0, "time": "20:00", "experience_id": "h1", "note": "夜景最佳"},
                {"day": 1, "time": "13:00", "experience_id": "p1", "note": "退房后去博物馆"},
            ], "reminders": [
                {"when": "入住前2天 20:00", "offset_minutes": -1680, "message": "下午茶需预约", "experience_id": "h0"},
            ]}
        raise AssertionError(system)

    def vision_json(self, image_url, prompt):
        return {"items": [{"title": "室内泳池", "evidence": "画面中有蓝色泳池"}]}


@pytest.fixture
def fly():
    return FlyaiClient(mode="fixture")


def test_parse_price():
    assert parse_price("¥618") == 618
    assert parse_price("¥721起/晚") == 721
    assert parse_price("¥2xx") is None
    assert parse_price("") is None


def test_ring_layers():
    assert ring_for(None) == ("距离待确认", None)
    assert ring_for(800)[0] == "步行圈"
    assert ring_for(8000)[0] == "车程圈"
    assert 1300 < haversine_m(31.2397, 121.4908, 31.2283, 121.4755) < 2200


def test_parse_intent_fills_weekend_dates():
    intent = parse_intent(FakeLLM(), "这周末和对象在上海住一晚", TODAY)
    assert next_saturday(TODAY) == dt.date(2026, 10, 17)
    assert (intent.check_in, intent.check_out) == ("2026-10-17", "2026-10-18")
    assert intent.budget == 1500


def test_mining_drops_unsupported_evidence(fly):
    hotel = fly.search_hotels("上海", "2026-10-17", "2026-10-18")[0]
    pkgs = fly.search_packages("上海")
    exps = mine_hotel_experiences(FakeLLM(), hotel, pkgs)
    assert [e.title for e in exps] == ["双人下午茶", "江景夜景"]  # “免费游艇”在原文中找不到，被丢弃


def test_rank_prefers_budget_and_persona(fly):
    hotels = fly.search_hotels("上海", "2026-10-17", "2026-10-18")
    pkgs = fly.search_packages("上海")
    kid = Intent(city="上海", check_in="2026-10-17", check_out="2026-10-18", persona="亲子", budget=1000)
    assert "亲子" in rank_hotels(hotels, pkgs, kid)[0].name


def test_checkout_time_from_package():
    assert checkout_time([Package(name="江景房+延迟退房至14:00", hotel_name="x")]) == "14:00"
    assert checkout_time([Package(name="江景房", hotel_name="x")]) == "12:00"


def _exps():
    return {
        "h0": Experience(id="h0", title="下午茶", source="套餐权益", ring="酒店内"),
        "p0": Experience(id="p0", title="博物馆", source="周边数据", ring="车程圈"),
    }


def test_validate_journey_rejects_invented_and_out_of_window():
    raw = {"timeline": [
        {"day": 0, "time": "10:00", "experience_id": "h0"},  # 早于入住
        {"day": 0, "time": "16:00", "experience_id": "h0"},
        {"day": 0, "time": "18:00", "experience_id": "ghost"},  # 不存在
        {"day": 1, "time": "13:00", "experience_id": "p0"},  # 周边体验可在退房后
    ], "reminders": [{"offset_minutes": "bad", "message": "x"}]}
    j = validate_journey(raw, "情侣", _exps(), "12:00")
    assert [(i.day, i.time, i.experience_id) for i in j.timeline] == [(0, "16:00", "h0"), (1, "13:00", "p0")]
    assert j.reminders == []


def test_validate_journey_too_short_returns_none():
    assert validate_journey({"timeline": []}, "情侣", _exps(), "12:00") is None


def test_fallback_journey_always_has_items():
    j = fallback_journey("情侣", list(_exps().values()), "12:00")
    assert len(j.timeline) == 2 and j.timeline[-1].time == "13:00"


def test_plan_end_to_end(fly):
    llm = FakeLLM()
    res = plan(llm, fly, Geocoder(key=""), "这周末和对象在上海住一晚，想看夜景喝下午茶", today=TODAY)
    assert res.hotel.name == "【样例】上海外滩江景酒店"
    sources = {e.source for e in res.experiences}
    assert {"套餐权益", "用户评价", "图片识别", "周边数据"} <= sources
    assert [j.persona for j in res.journeys] == ["情侣", "亲子"]
    assert res.journeys[0].timeline[0].experience_id == "h0"
    near = [e for e in res.experiences if e.source == "周边数据"]
    assert near[0].ring == "步行圈" and near[0].bearing is not None
    assert any("泳池" in c.question for c in res.to_confirm)


def test_hotel_parses_live_marriott_fields():
    h = FlyaiClient._hotel({"name": "上海明捷万丽酒店", "shid": "54804118", "rate": None,
                            "nearbyPoi": "近南京路步行街", "star": "豪华型", "price": "¥721起/晚"})
    assert (h.id, h.interests_poi, h.price, h.score) == ("54804118", "近南京路步行街", 721, None)


def test_rank_respects_min_star():
    cheap = Hotel(id="1", name="高档酒店", star="高档型", price=306)
    lux = Hotel(id="2", name="豪华酒店", star="豪华型", price=1200)
    five = Intent(city="上海", check_in="2026-10-31", check_out="2026-11-01", persona="亲子",
                  budget=2000, min_star=5)
    assert rank_hotels([cheap, lux], [], five)[0].name == "豪华酒店"


def test_live_package_matches_hotel_by_title():
    pkg = Package(name="上海漕河泾万丽酒店豪华房1晚+2大1小自助早餐", hotel_name="")
    group = Package(name="万豪集团450+店3晚 可拆分多档通兑", hotel_name="")
    hotel = Hotel(id="1", name="上海漕河泾万丽酒店")
    assert packages_of(hotel, [pkg, group]) == [pkg]
