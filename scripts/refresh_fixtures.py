"""用飞猪 flyai 线上真实数据刷新 fixtures/（离线演示与测试用）。

用法：python scripts/refresh_fixtures.py [城市] [入住] [退房] [演示酒店]
默认：上海 2026-10-31 2026-11-01 上海漕河泾万丽酒店

每个 fixture 文件是“城市级查询 + 演示酒店定向查询”的合并结果，保持 flyai 原始字段不做改写，
这样离线模式和线上走的是同一套字段解析逻辑。演示酒店需有万豪套餐，离线时才能演示套餐权益挖掘。
"""
import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.flyai import FlyaiClient  # noqa: E402
from app.pipeline import PERSONA_POI, WANT_POI  # noqa: E402

city, check_in, check_out, demo_hotel = (sys.argv[1:] + [None] * 4)[:4]
city = city or "上海"
check_in = check_in or "2026-10-31"
check_out = check_out or "2026-11-01"
demo_hotel = demo_hotel or "上海漕河泾万丽酒店"

# 用临时缓存目录，确保拿到的是本次实时数据
fly = FlyaiClient(mode="live", cache_dir=Path(tempfile.mkdtemp()))


def merged(command: str, calls: list[dict], key: str) -> dict:
    items, seen = [], set()
    for opts in calls:
        for i in fly.items(fly.run(command, **opts)):
            k = i.get(key) or i.get("name") or i.get("title")
            if k not in seen:
                seen.add(k)
                items.append(i)
    print(f"{command}: {len(items)} 条")
    return {"status": 0, "message": "success", "data": {"itemList": items},
            "_fixture": {"fetched_at": dt.date.today().isoformat(), "city": city,
                         "check_in": check_in, "check_out": check_out, "demo_hotel": demo_hotel}}


dates = {"check_in_date": check_in, "check_out_date": check_out}
fixtures = {
    "search-hotel": merged("search-hotel", [
        {"dest_name": city, "hotel_stars": "4,5", "sort": "rate_desc", **dates},
        {"dest_name": city, "key_words": demo_hotel, **dates},
    ], "shId"),
    "search-marriott-hotel": merged("search-marriott-hotel", [
        {"dest_name": city, "sort": "rate_desc", **dates},
    ], "shid"),
    "search-marriott-package": merged("search-marriott-package", [
        {"province_or_city": city},
        {"province_or_city": city, "hotel_name": demo_hotel},
    ], "itemId"),
    # POI 按类别分组保存，离线时不同类别返回不同结果；每类保留前 6 条够流水线使用
    "search-poi": {"variant_by": "category", "variants": {
        c: {**(d := merged("search-poi", [{"city_name": city, "category": c}], "id")),
            "data": {"itemList": d["data"]["itemList"][:6]}}
        for c in dict.fromkeys([*WANT_POI.values(), *(c for cs in PERSONA_POI.values() for c in cs)])
    }},
}


def masked(data: dict) -> bool:
    """价格字段里出现 x 说明被脱敏（图片 URL 里的 xx 不算）。"""
    groups = data["variants"].values() if "variants" in data else [data]
    return any("x" in str(i.get("price") or "") for g in groups for i in fly.items(g))


for name, data in fixtures.items():
    if masked(data):
        sys.exit(f"{name} 价格被脱敏，FLYAI_API_KEY 未生效，未写入")
    text = json.dumps(data, ensure_ascii=False, indent=1)
    (settings.fixture_dir / f"{name}.json").write_text(text + "\n", encoding="utf-8")
print("已写入", settings.fixture_dir)
