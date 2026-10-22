"""飞猪 flyai CLI 封装：调用、缓存、火车 / 航班结果归一化。"""

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .config import settings
from .schemas import Leg


class FlyaiError(RuntimeError):
    pass


TRANSIENT = re.compile(r"HTTP 5\d\d|Service Unavailable|DNS|ECONNRESET|ETIMEDOUT")


def _float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class FlyaiClient:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or settings.cache_dir / "flyai"

    # ---- 底层调用 ----
    def run(self, command: str, **options) -> dict:
        """相同参数的结果缓存到 .cache/flyai/：飞猪 15 分钟内约 90 次请求就会触发风控。"""
        args = [command]
        for k, v in options.items():
            if v is None or v == "":
                continue
            args += [f"--{k.replace('_', '-')}", str(v)]

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

    @staticmethod
    def items(data: dict) -> list[dict]:
        return (data.get("data") or {}).get("itemList") or []

    # ---- 业务查询 ----
    # 每次只返回 10 条且按出发时间排序，不给出发时段的话全是清晨的车次
    def search_trains(self, origin: str, destination: str, date: str, after_hour: int | None = None) -> list[Leg]:
        data = self.run("search-train", origin=origin, destination=destination, dep_date=date,
                        dep_hour_start=after_hour)
        return [leg for i in self.items(data) if (leg := _leg(i, "火车"))]

    def search_flights(self, origin: str, destination: str, date: str, after_hour: int | None = None) -> list[Leg]:
        data = self.run("search-flight", origin=origin, destination=destination, dep_date=date,
                        dep_hour_start=after_hour)
        return [leg for i in self.items(data) if (leg := _leg(i, "航班"))]


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
