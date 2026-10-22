import datetime as dt

from test_route import US, FakeLLM, TODAY, fake_geo

from app.ticket import read_tickets
from app.transit import default_rules
from app.travelmate import apply_ticket, assess, parse_request

R = default_rules()


def seg(no, dep, dep_iata, dep_city, dep_country, arr, arr_iata, arr_city, arr_country, arr_date=None, arr_time=None,
        dep_date=None, dep_time=None):
    return {"flight_no": no, "dep_airport": dep, "dep_iata": dep_iata, "dep_city": dep_city, "dep_country": dep_country,
            "dep_date": dep_date, "dep_time": dep_time, "arr_airport": arr, "arr_iata": arr_iata,
            "arr_city": arr_city, "arr_country": arr_country, "arr_date": arr_date, "arr_time": arr_time}


EN_TICKET = {"is_ticket": True, "segments": [
    seg("MU586", "LOS ANGELES", "LAX", "洛杉矶", "美国", "SHANGHAI PUDONG", "PVG", "上海", "中国",
        dep_date="2026-10-25", dep_time="23:50", arr_date="2026-10-27", arr_time="05:40"),
    seg("3U3961", "CHENGDU TIANFU", "TFU", "成都", "中国", "SEOUL INCHEON", "ICN", "首尔", "韩国",
        dep_date="2026-11-04", dep_time="13:30", arr_date="2026-11-04", arr_time="18:40")]}
ZH_TICKET = {"is_ticket": True, "segments": [
    seg("KE018", "洛杉矶国际机场", "LAX", "洛杉矶", "美国", "仁川国际机场", "ICN", "首尔", "韩国",
        dep_date="2026-10-25", dep_time="23:30", arr_date="2026-10-27", arr_time="05:10"),
    seg("KE893", "仁川国际机场", "ICN", "首尔", "韩国", "首都国际机场", "PEK", "北京", "中国",
        dep_date="2026-10-27", dep_time="08:40", arr_date="2026-10-27", arr_time="09:45"),
    seg("CA925", "首都国际机场", "PEK", "北京", "中国", "成田国际机场", "NRT", "东京", "日本",
        dep_date="2026-11-05", dep_time="08:20", arr_date="2026-11-05", arr_time="12:35")]}
# 实测：登机牌的 BOARDING 12:40 / DEPARTURE 13:20 被读成起飞 / 到达
BOARDING = {"is_ticket": True, "segments": [
    seg("KE893", "ICN", "ICN", "首尔", "韩国", "PVG", "PVG", "上海", "中国",
        dep_date="2026-10-26", dep_time="12:40", arr_date="2026-10-26", arr_time="13:20")]}


class VisionLLM(FakeLLM):
    """文字解析返回 reply；看图按图片内容返回预置结果。"""

    def __init__(self, reply, images):
        super().__init__(reply)
        self.images = images
        self.seen = []

    def vision_json(self, image, prompt):
        self.seen.append(prompt)
        r = self.images[image]
        if isinstance(r, Exception):
            raise r
        return r


def tickets(*raws):
    llm = VisionLLM({}, {f"data:image/png;base64,{i}": r for i, r in enumerate(raws)})
    return read_tickets(llm, list(llm.images), TODAY), llm


# ---- 识别与判定 ----

def test_entry_exit_origin_destination():
    t, _ = tickets(EN_TICKET)
    assert (t.entry.flight_no, t.exit.flight_no) == ("MU586", "3U3961")
    assert t.entry.arr_time == dt.datetime(2026, 10, 27, 5, 40)  # 跨日到达
    assert (t.origin, t.destination, t.destination_city) == ("美国", "韩国", "首尔")


def test_connection_traced_back_to_true_origin():
    """洛杉矶→首尔→北京：出发地是美国，不是转机的韩国（否则会误判成别的来源）。"""
    t, _ = tickets(ZH_TICKET)
    assert t.entry.flight_no == "KE893" and t.origin == "美国" and t.destination == "日本"
    assert any("Seoul" in n or "首尔" in n for n in t.notes_en + t.notes)


def test_boarding_pass_implausible_arrival_dropped():
    t, _ = tickets(BOARDING)
    assert t.entry.arr_time is None and t.exit is None
    assert t.notes  # 说明到达时间是估的


def test_two_screenshots_merged_and_deduped():
    t, _ = tickets(EN_TICKET, EN_TICKET)
    assert [s.flight_no for s in t.segments] == ["MU586", "3U3961"]


def test_bad_segments_and_failures():
    from app.llm import LLMError
    junk = {"segments": [{"flight_no": "hello world", "dep_date": "2026-10-26"},
                         {"flight_no": "MU586", "dep_date": "not a date"}, "garbage"]}
    t, _ = tickets(junk, LLMError("timeout"))
    assert t.segments == [] and t.unreadable == 2 and t.entry is None
    t, _ = tickets({"is_ticket": False, "segments": []})
    assert t.unreadable == 1


def test_year_rolled_forward():
    old = {"segments": [seg("MU586", "LAX", "LAX", "洛杉矶", "美国", "PVG", "PVG", "上海", "中国",
                            dep_date="2025-10-25", dep_time="23:50", arr_date="2025-10-27", arr_time="05:40")]}
    t, _ = tickets(old)
    assert t.entry.arr_time.year == 2026


def test_prompt_has_today_and_boarding_hint():
    _, llm = tickets(EN_TICKET)
    assert "2026-10-17" in llm.seen[0] and "BOARDING" in llm.seen[0]


def test_at_most_two_images_read():
    _, llm = tickets(EN_TICKET, ZH_TICKET, BOARDING)
    assert len(llm.seen) == 2


# ---- 合并进行程需求 ----

def test_ticket_overrides_text_and_notes_conflict():
    req = parse_request(FakeLLM({**US, "entry_date": "2026-10-26", "entry_time": "14:30", "exit_time": None}),
                        "landing at 2:30pm", TODAY)
    t, _ = tickets(EN_TICKET)
    apply_ticket(req, t, R)
    assert req.entry_port == "上海浦东国际机场" and req.entry_time == dt.datetime(2026, 10, 27, 5, 40)
    assert req.exit_port == "成都天府国际机场" and req.exit_time == dt.datetime(2026, 11, 4, 13, 30)
    assert req.exit_time_known and req.destination_city == "首尔"
    assert any("used the ticket" in x for x in req.assumptions_en)
    assert not any(x.startswith("离境时刻") for x in req.assumptions)  # 机票上有时刻，旧的假设撤掉
    assert len(req.assumptions) == len(req.assumptions_en) and len(req.missing) == len(req.missing_en)


def test_screenshot_only_still_asks_nationality():
    geo = fake_geo()
    llm = VisionLLM({}, {"data:image/png;base64,0": EN_TICKET})
    a = assess(llm, geo, "", TODAY, images=["data:image/png;base64,0"])
    assert a.request.missing == ["国籍"] and a.request.entry_port == "上海浦东国际机场"
    assert a.verdict is None and a.ticket.entry.flight_no == "MU586"


def test_end_to_end_with_ticket():
    geo = fake_geo()
    reply = {"nationality": "美国", "places": [{"text": "pandas", "zh": "成都大熊猫繁育研究基地"}]}
    llm = VisionLLM(reply, {"data:image/png;base64,0": EN_TICKET})
    a = assess(llm, geo, "I'm American. Want the pandas.", TODAY, images=["data:image/png;base64,0"])
    v = a.verdict
    assert a.request.missing == [] and v.ok and v.policy == "transit_240h"
    assert v.deadline == dt.datetime(2026, 11, 7, 0, 0)  # 10-27 落地 → 10-28 0 时起算
    assert v.minutes_to_spare == (v.deadline - dt.datetime(2026, 11, 4, 13, 30)).total_seconds() // 60


def test_ticket_reveals_round_trip_to_origin():
    """机票显示回美国：即使文字没提，也会按 R2 拦下。"""
    back = {"segments": [EN_TICKET["segments"][0],
                         seg("MU587", "PVG", "PVG", "上海", "中国", "LAX", "LAX", "洛杉矶", "美国",
                             dep_date="2026-11-03", dep_time="12:00")]}
    llm = VisionLLM({"nationality": "美国", "places": []}, {"data:image/png;base64,0": back})
    a = assess(llm, fake_geo(), "American", TODAY, images=["data:image/png;base64,0"])
    assert not a.verdict.ok and any(i.rule == "R2" and i.level == "block" for i in a.verdict.issues)
