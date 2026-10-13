"""阿里云百炼（DashScope OpenAI 兼容接口）调用封装。"""

import json
import re
from typing import Protocol

import httpx

from .config import settings


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def chat_json(self, system: str, user: str) -> dict: ...
    def vision_json(self, image_url: str, prompt: str) -> dict: ...


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise LLMError(f"模型未返回 JSON：{text[:200]}")
    return json.loads(text[start:end + 1])


class Bailian:
    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 text_model: str | None = None, vl_model: str | None = None,
                 client: httpx.Client | None = None):
        self.api_key = api_key or settings.dashscope_api_key
        if not self.api_key:
            raise LLMError("缺少 DASHSCOPE_API_KEY：本作品的理解与生成环节运行在阿里云百炼上，请在 .env 中配置")
        self.base_url = (base_url or settings.bailian_base_url).rstrip("/")
        self.text_model = text_model or settings.text_model
        self.vl_model = vl_model or settings.vl_model
        self.client = client or httpx.Client(timeout=90)

    def _complete(self, model: str, messages: list[dict], json_mode: bool) -> str:
        body: dict = {"model": model, "messages": messages, "temperature": 0.3}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = self.client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
        )
        if resp.status_code != 200:
            raise LLMError(f"百炼调用失败 {resp.status_code}：{resp.text[:300]}")
        return resp.json()["choices"][0]["message"]["content"]

    def chat_json(self, system: str, user: str) -> dict:
        content = self._complete(
            self.text_model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=True,
        )
        return _extract_json(content)

    def vision_json(self, image_url: str, prompt: str) -> dict:
        content = self._complete(
            self.vl_model,
            [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": prompt + "\n只输出 JSON。"},
            ]}],
            json_mode=False,  # VL 模型不保证支持 json_object，靠解析兜底
        )
        return _extract_json(content)
