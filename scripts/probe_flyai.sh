#!/usr/bin/env bash
# W1 数据实测：验证 Staycation 方案依赖的 flyai 字段是否够用。
# 用法：npm i -g @fly-ai/flyai-cli && flyai config set FLYAI_API_KEY "<key>"
#       bash scripts/probe_flyai.sh 上海 2026-10-31 2026-11-01
set -u
CITY="${1:-上海}"
IN="${2:-2026-10-31}"
OUT="${3:-2026-11-01}"
DIR="probe_out/${CITY}"
mkdir -p "$DIR"

run() { # name, command...
  local name="$1"; shift
  echo ">> $name"
  "$@" > "$DIR/$name.json" 2> "$DIR/$name.err" || echo "   失败，见 $DIR/$name.err"
}

run hotel_5star      flyai search-hotel --dest-name "$CITY" --hotel-stars 5 --check-in-date "$IN" --check-out-date "$OUT" --sort rate_desc
run marriott_hotel   flyai search-marriott-hotel --dest-name "$CITY" --check-in-date "$IN" --check-out-date "$OUT"
run marriott_package flyai search-marriott-package --province-or-city "$CITY"
run kw_package       flyai keyword-search --query "${CITY} 酒店套餐 亲子"
run kw_spa           flyai keyword-search --query "${CITY} 酒店 SPA 下午茶 套餐"
run ai_facility      flyai ai-search --query "${CITY}有室内恒温泳池和儿童俱乐部的五星酒店，周末入住"
run kw_events        flyai keyword-search --query "${CITY} 本周末 演出 展览"
run poi_nearby       flyai search-poi --city-name "$CITY" --category 博物馆

# 汇总：需要关注的问题
#  1. 价格是否脱敏（出现 xx 说明 Key 没生效）
#  2. marriott_package 的 sellingPoint 能否抽出具体权益
#  3. hotel 的 review / interestsPoi 是否能反映设施（泳池、SPA、亲子）
#  4. ai_facility 是否真的按设施筛选
python3 - "$DIR" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
for f in sorted(d.glob("*.json")):
    try:
        items = json.loads(f.read_text() or "{}").get("data", {}).get("itemList", [])
    except Exception as e:
        print(f"{f.stem}: 解析失败 {e}"); continue
    masked = sum("x" in str(i.get("price", i.get("info", {}).get("price", ""))) for i in items)
    keys = sorted({k for i in items for k in (i.get("info", i)).keys()})
    print(f"{f.stem}: {len(items)} 条, 价格脱敏 {masked} 条, 字段 {keys}")
    for i in items[:3]:
        x = i.get("info", i)
        print("   -", x.get("name") or x.get("title"), "|", x.get("price"), "|", x.get("sellingPoint") or x.get("review") or x.get("tags"))
PY
