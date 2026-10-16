"""实测入境游客免签评估全链路（百炼解析 + 高德定位 + 规则引擎）。用法：python scripts/probe_travelmate.py"""
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.geo import Geocoder  # noqa: E402
from app.llm import Bailian  # noqa: E402
from app.travelmate import assess  # noqa: E402

CASES = [
 "I'm American, landing at Shanghai Pudong on Oct 26 at 2:30pm from LA, flying to Seoul on Nov 4. Want to see Shanghai, Hangzhou, the pandas in Chengdu and Jiuzhaigou.",
 "US passport. Arriving Beijing Capital Nov 1 morning from San Francisco, leaving from Xi'an to Tokyo Nov 12. Great Wall, Forbidden City, Terracotta Warriors.",
 "Hi! Mexican citizen, flying Mexico City -> Guangzhou on Dec 3, then back home Dec 10 from Guangzhou. Want Guilin and Yangshuo.",
 "I'm German, coming to Kunming from Bangkok next week, want Dali, Lijiang and Shangri-La, then fly to Singapore.",
]
llm, geo = Bailian(text_model=os.environ.get("TM_MODEL", "qwen-plus")), Geocoder()
for t in CASES:
    s=time.time(); a = assess(llm, geo, t, dt.date.today()); dt_=time.time()-s
    r=a.request
    print(f"\n### {t[:70]}…  ({dt_:.1f}s)")
    print(f" 国籍={r.nationality} 从={r.origin} 去={r.destination} 入境={r.entry_port} {r.entry_time} 离境={r.exit_port} {r.exit_time}")
    print(" 地点:", "; ".join(f"{s.ask.text}→{s.ask.zh}→{(s.place.province+s.place.city) if s.place else '未定位'}" for s in a.stops))
    if r.missing: print(" 缺:", r.missing)
    if r.assumptions: print(" 假设:", r.assumptions)
    if a.verdict:
        v=a.verdict; print(f" 结论: ok={v.ok} 政策={v.policy} 截止={v.deadline} 余={v.minutes_to_spare}分")
        for i in v.issues: print(f"   [{i.level}] {i.rule} {i.message}")
