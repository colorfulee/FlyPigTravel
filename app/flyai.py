"""飞猪 flyai CLI 封装：调用、缓存、离线样例、字段归一化。"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .config import settings
from .schemas import Hotel, Package, Poi


class FlyaiError(RuntimeError):
    pass


def parse_price(text: str | None) -> int | None:
    """'¥618' -> 618；脱敏价格（'¥2xx'）或空值返回 None。"""
    if not text:
        return None
    m = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:起)?\s*(?:/晚)?", str(text).strip())
    return int(float(m.group(1))) if m else None


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class FlyaiClient:
    def __init__(self, mode: str | None = None, cache_dir: Path | None = None):
        self.mode = mode or settings.flyai_mode
        self.cache_dir = cache_dir or settings.cache_dir / "flyai"

    # ---- 底层调用 ----
    def run(self, command: str, **options) -> dict:
        args = [command]
        for k, v in options.items():
            if v is None or v == "":
                continue
            args += [f"--{k.replace('_', '-')}", str(v)]

        if self.mode == "fixture":
            return self._fixture(command)

        key = hashlib.sha1(json.dumps(args, ensure_ascii=False).encode()).hexdigest()[:16]
        cache = self.cache_dir / f"{command}-{key}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))

        env = {**os.environ}
        if settings.flyai_api_key:
            env["FLYAI_API_KEY"] = settings.flyai_api_key
        try:
            proc = subprocess.run(
                [settings.flyai_bin, *args], capture_output=True, text=True, timeout=60, env=env
            )
        except FileNotFoundError as e:
            raise FlyaiError("未找到 flyai 命令，请先 npm i -g @fly-ai/flyai-cli") from e
        except subprocess.TimeoutExpired as e:
            raise FlyaiError(f"flyai {command} 超时") from e
        if proc.returncode != 0:
            raise FlyaiError(f"flyai {command} 失败：{proc.stderr.strip()[:300]}")
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as e:
            raise FlyaiError(f"flyai {command} 返回无法解析：{proc.stdout[:200]}") from e
        if data.get("status") not in (0, "0", None):
            raise FlyaiError(f"flyai {command} 状态异常：{data.get('message')}")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def _fixture(self, command: str) -> dict:
        path = settings.fixture_dir / f"{command}.json"
        if not path.exists():
            return {"data": {"itemList": []}, "status": 0}
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def items(data: dict) -> list[dict]:
        return (data.get("data") or {}).get("itemList") or []

    # ---- 业务查询 ----
    def search_hotels(self, city: str, check_in: str, check_out: str,
                      max_price: int | None = None, keywords: str | None = None,
                      stars: str | None = "4,5") -> list[Hotel]:
        data = self.run(
            "search-hotel", dest_name=city, check_in_date=check_in, check_out_date=check_out,
            hotel_stars=stars, max_price=max_price, key_words=keywords, sort="rate_desc",
        )
        return [self._hotel(i) for i in self.items(data)]

    def search_marriott_hotels(self, city: str, check_in: str, check_out: str,
                               max_price: int | None = None) -> list[Hotel]:
        data = self.run(
            "search-marriott-hotel", dest_name=city, check_in_date=check_in,
            check_out_date=check_out, max_price=max_price, sort="rate_desc",
        )
        return [self._hotel(i, brand_default="万豪") for i in self.items(data)]

    def search_packages(self, city: str, hotel_name: str | None = None) -> list[Package]:
        data = self.run("search-marriott-package", province_or_city=city, hotel_name=hotel_name)
        return [
            Package(
                name=i.get("name") or i.get("title") or "", hotel_name=i.get("hotelName") or "",
                price_text=str(i.get("price") or ""),
                selling_point=i.get("sellingPoint") or i.get("sellPoint") or i.get("benefit") or "",
                pic=i.get("mainPic") or i.get("picUrl") or "", url=i.get("detailUrl") or "",
            )
            for i in self.items(data)
        ]

    def search_pois(self, city: str, category: str | None = None,
                    keyword: str | None = None) -> list[Poi]:
        data = self.run("search-poi", city_name=city, category=category, keyword=keyword)
        out = []
        for i in self.items(data):
            ticket = i.get("ticketInfo") or {}
            out.append(Poi(
                id=str(i.get("id") or i.get("name")), name=i.get("name", ""),
                address=i.get("address") or "", category=category or "",
                ticket_price=str(ticket["price"]) if ticket.get("price") else None,
                pic=i.get("mainPic") or i.get("picUrl") or "", url=i.get("jumpUrl") or "",
                lat=_float(i.get("latitude")), lng=_float(i.get("longitude")),
            ))
        return out

    @staticmethod
    def _hotel(i: dict, brand_default: str = "") -> Hotel:
        return Hotel(
            id=str(i.get("shId") or i.get("shid") or i.get("name")), name=i.get("name", ""),
            price=parse_price(i.get("price")), price_text=str(i.get("price") or ""),
            score=_float(i.get("score") or i.get("rate")), star=i.get("star") or "",
            review=i.get("review") or "",
            interests_poi=i.get("interestsPoi") or i.get("nearbyPoi") or "",
            address=i.get("address") or "", lat=_float(i.get("latitude")),
            lng=_float(i.get("longitude")), pic=i.get("mainPic") or "",
            url=i.get("detailUrl") or "", brand=i.get("brandName") or brand_default,
        )
