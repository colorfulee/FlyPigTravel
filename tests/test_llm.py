import json

import httpx
import pytest

from app.llm import Bailian, LLMError


def _client(captured, content):
    def handler(request: httpx.Request):
        captured.append(json.loads(request.content))
        captured.append(request.headers["authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_chat_json_uses_bailian_compatible_api():
    cap = []
    b = Bailian(api_key="k", base_url="https://dashscope.example/v1", text_model="qwen-plus",
                client=_client(cap, '{"a": 1}'))
    assert b.chat_json("sys", "hi") == {"a": 1}
    assert cap[0]["model"] == "qwen-plus" and cap[0]["response_format"] == {"type": "json_object"}
    assert cap[1] == "Bearer k"


def test_vision_json_parses_fenced_output():
    cap = []
    b = Bailian(api_key="k", vl_model="qwen3-vl-plus", client=_client(cap, '```json\n{"items": []}\n```'))
    assert b.vision_json("https://img", "看图") == {"items": []}
    assert cap[0]["messages"][0]["content"][0]["image_url"]["url"] == "https://img"


def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.setattr("app.llm.settings", type("S", (), {"dashscope_api_key": ""})())
    with pytest.raises(LLMError, match="DASHSCOPE_API_KEY"):
        Bailian()
