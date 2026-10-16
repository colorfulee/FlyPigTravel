"""飞猪 flyai CLI 封装：调用、缓存、离线样例、字段归一化。"""

import hashlib
import json
import os
import re
import datetime as dt
import subprocess
from pathlib import Path

from .config import settings
from .schemas import Hotel, Leg, Package, Poi


class FlyaiError(RuntimeError):
    pass


TRANSIENT = re.compile(r"HTTP 5\d\d|Service Unavailable|DNS|ECONNRESET|ETIMEDOUT")


def parse_price(text: str | None) -> int | None:
    """'¥618' -> 618；脱敏价格（'¥2xx'）或空值返回 None。"""
    if not text:
        return None
    m = re.fullmatch(r"[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:起)?\s*(?:/晚)?", str(text).strip())
    return int(float(m.group(1))) if m else None


def _year(v) -> int | None:
    m = re.match(r"(19|20)\d\d", str(v or ""))
    return int(m.group()) if m else None


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
            return self._fixture(command, options)

        key = hashlib.sha1(json.dumps(args, ensure_ascii=False).encode()).hexdigest()[:16]
        cache = self.cache_dir / f"{command}-{key}.json"
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))

        env = {**os.environ}
        if settings.flyai_api_key:
            env["FLYAI_API_KEY"] = settings.flyai_api_key
        for attempt in range(2):  # 线上偶发 503 / DNS 失败，重试一次
            try:
                proc = subprocess.run(
                    [settings.flyai_bin, *args], capture_output=True, text=True, timeout=60, env=env
                )
            except FileNotFoundError as e:
                raise FlyaiError("未找到 flyai 命令，请先 npm i -g @fly-ai/flyai-cli") from e
            except subprocess.TimeoutExpired as e:
                raise FlyaiError(f"flyai {command} 超时") from e
            if proc.returncode == 0 or not TRANSIENT.search(proc.stderr):
                break
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

    def _fixture(self, command: str, options: dict) -> dict:
        empty = {"data": {"itemList": []}, "status": 0}
        path = settings.fixture_dir / f"{command}.json"
        if not path.exists():
            return empty
        data = json.loads(path.read_text(encoding="utf-8"))
        if "variants" in data:  # 按某个参数分组的样例，如 search-poi 按 category
            return data["variants"].get(str(options.get(data["variant_by"]) or ""), empty)
        return data

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

    # 每次只返回 10 条且按出发时间排序，不给出发时段的话全是清晨的车次
    def search_trains(self, origin: str, destination: str, date: str, after_hour: int | None = None) -> list[Leg]:
        data = self.run("search-train", origin=origin, destination=destination, dep_date=date,
                        dep_hour_start=after_hour)
        return [leg for i in self.items(data) if (leg := _leg(i, "火车"))]

    def search_flights(self, origin: str, destination: str, date: str, after_hour: int | None = None) -> list[Leg]:
        data = self.run("search-flight", origin=origin, destination=destination, dep_date=date,
                        dep_hour_start=after_hour)
        return [leg for i in self.items(data) if (leg := _leg(i, "航班"))]

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
            decorated=_year(i.get("decorationTime")),
        )


def _leg(item: dict, mode: str) -> Leg | None:
    """journeys[0].segments 是直达 1 段或中转多段；字段缺失的条目丢弃。"""
    try:
        segs = item["journeys"][0]["segments"]
        first, last = segs[0], segs[-1]
        dep = dt.datetime.fromisoformat(first["depDateTime"])
        arr = dt.datetime.fromisoformat(last["arrDateTime"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    minutes = int(_float(item.get("totalDuration")) or (arr - dep).total_seconds() // 60)
    return Leg(
        mode=mode, no=" + ".join(s.get("marketingTransportNo") or "?" for s in segs),
        dep_station=first.get("depStationName") or "", dep_code=first.get("depStationCode") or "",
        arr_station=last.get("arrStationName") or "", dep_time=dep, arr_time=arr, minutes=minutes,
        transfers=len(segs) - 1, price=_float(item.get("price") or item.get("ticketPrice")),
        url=item.get("jumpUrl") or "",
    )
