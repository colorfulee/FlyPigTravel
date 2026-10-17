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
