"""实测一句英文需求 → 免签判定 → 按城市排程 + 飞猪火车/航班。每次约 2-5 次 flyai 调用。
用法：python scripts/probe_route.py "I'm American, landing at Shanghai Pudong on Oct 26 at 2:30pm ..."
"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.flyai import FlyaiClient  # noqa: E402
from app.geo import Geocoder  # noqa: E402
from app.llm import Bailian  # noqa: E402
from app.route import plan_route  # noqa: E402
from app.travelmate import assess  # noqa: E402

text = sys.argv[1]
llm, geo, fly = Bailian(), Geocoder(), FlyaiClient()
t=time.time(); a = assess(llm, geo, text, dt.date.today()); t1=time.time()-t
t=time.time(); it = plan_route(a, fly, geo); t2=time.time()-t
print(f"解析 {t1:.1f}s，排程 {t2:.1f}s，flyai 调用 {it.flyai_calls} 次")
print("移出行程:", it.skipped)
for i,c in enumerate(it.cities):
    print(f" {c.arrive} 到 {c.name}（{c.province}{c.city}）住 {c.nights} 晚：{'、'.join(c.places) or '—'}")
    if i < len(it.legs):
        l=it.legs[i]; print("     →", f"{l.mode} {l.no} {l.dep_station} {l.dep_time:%m-%d %H:%M} → {l.arr_station} {l.arr_time:%H:%M}（{l.minutes} 分，¥{l.price}）" if l else "未查到")
e=it.exit_leg
print(" 离境:", f"{e.mode} {e.no} {e.dep_station}({e.dep_code}) {e.dep_time:%m-%d %H:%M} → {e.arr_station}（¥{e.price}）" if e else ("已订" if it.exit_leg_booked else "未查到"))
v=it.verdict
if v:
    print(f" 最终结论 ok={v.ok} 截止={v.deadline} 余={v.minutes_to_spare} 分")
    for i in v.issues:
        print(f"   [{i.level}] {i.rule} {i.message}")
for i in it.issues: print(f"   排程[{i.level}] {i.message}")
