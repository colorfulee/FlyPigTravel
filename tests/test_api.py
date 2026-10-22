"""接口层：流式与一次性两种返回结果一致，阶段顺序正确，出错时流里给出 error 而不是断开。"""
import json

import pytest
from fastapi.testclient import TestClient
from test_route import US, US_FLY, FakeFly, FakeLLM, fake_geo

import app.main as main
from app.llm import LLMError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "llm", lambda: FakeLLM(US))
    monkeypatch.setattr(main, "geo", fake_geo())
    monkeypatch.setattr(main, "fly", FakeFly(US_FLY))
    monkeypatch.setattr(main, "SNAPSHOT_DIR", tmp_path)
    return TestClient(main.app)


def lines(r):
    return [json.loads(x) for x in r.text.splitlines() if x.strip()]


def test_stream_stages_in_order(client):
    evs = lines(client.post("/api/transit/stream", json={"text": "landing at 2:30pm"}))
    assert [e["stage"] for e in evs] == ["assessment", "itinerary", "alternatives", "done"]
    assert evs[0]["assessment"]["verdict"]["policy"] == "transit_240h"
    assert evs[1]["itinerary"]["exit_leg"]["no"] == "3U3961" and evs[1]["reminders"] and "BEGIN:VCALENDAR" in evs[1]["ics"]


def test_stream_and_full_agree_and_snapshot_saved(client, tmp_path):
    full = client.post("/api/transit", json={"text": "landing at 2:30pm"}).json()
    evs = lines(client.post("/api/transit/stream", json={"text": "landing at 2:30pm"}))
    assert full["itinerary"] == evs[1]["itinerary"] and full["reminders"] == evs[1]["reminders"]
    snap = client.get("/api/transit/snapshot").json()
    assert snap["itinerary"] == full["itinerary"]
    assert len(list(tmp_path.glob("transit-*.json"))) == 2  # 按需求一份 + last


def test_stream_reports_llm_error(client, monkeypatch):
    class Boom:
        def chat_json(self, s, u):
            raise LLMError("429")
    monkeypatch.setattr(main, "llm", lambda: Boom())
    evs = lines(client.post("/api/transit/stream", json={"text": "hi"}))
    assert evs == [{"stage": "error", "detail": "Our AI service is busy, please try again (429)"}]


@pytest.mark.parametrize("text, code", [("  ", 400), ("x" * 2001, 413)])
def test_input_validation(client, text, code):
    assert client.post("/api/transit/stream", json={"text": text}).status_code == code
    assert client.post("/api/transit", json={"text": text}).status_code == code


@pytest.mark.parametrize("images, code", [(["http://x/y.png"], 400), (["data:image/png;base64,a"] * 3, 400),
                                          (["data:image/png;base64," + "a" * 8_000_001], 413)])
def test_screenshot_validation(client, images, code):
    assert client.post("/api/transit/stream", json={"text": "hi", "images": images}).status_code == code


def test_screenshot_without_text_is_accepted(client, monkeypatch):
    from test_ticket import EN_TICKET, VisionLLM
    monkeypatch.setattr(main, "llm", lambda: VisionLLM({}, {"data:image/png;base64,0": EN_TICKET}))
    evs = lines(client.post("/api/transit/stream", json={"text": "", "images": ["data:image/png;base64,0"]}))
    a = evs[0]["assessment"]
    assert a["ticket"]["entry"]["flight_no"] == "MU586" and a["request"]["missing"] == ["国籍"]


def test_missing_keys_reported_before_streaming(client, monkeypatch):
    from app.geo import Geocoder
    monkeypatch.setattr(main, "geo", Geocoder(key=""))
    r = client.post("/api/transit/stream", json={"text": "hi"})
    assert r.status_code == 503 and "AMAP_KEY" in r.json()["detail"]

    def no_key():
        raise main.HTTPException(503, "缺少 DASHSCOPE_API_KEY")
    monkeypatch.setattr(main, "llm", no_key)
    r = client.post("/api/transit", json={"text": "hi"})
    assert r.status_code == 503 and "DASHSCOPE_API_KEY" in r.json()["detail"]


def test_beijing_today_is_a_date():
    import datetime as dt
    assert abs((main.beijing_today() - dt.date.today()).days) <= 1
