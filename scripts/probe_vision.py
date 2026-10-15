"""VisionTour 可行性实测：qwen3-vl-plus 能否从景区照片认出是哪个景点。

用法：python scripts/probe_vision.py [样本数，默认 10]
样本：fixtures/search-poi.json 里飞猪景点的主图，景点名即标准答案。
两组对比：
  盲识别     只给照片，让模型直接说出景点名
  候选选择   另给 5 个候选（含正确答案），模拟“定位拿到附近景点，再由模型看图选一个”
注意：候选是从全市景点随机抽的，真实场景下附近景点更相似，准确率会低一些。
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.llm import Bailian, LLMError  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10
BLIND = ('请分析这张景区照片，只输出JSON：{"landmark":景点/地标名称（不确定写"未知"）,'
         '"confidence":0-1,"spots":[{"tip":"机位或姿势建议","why":"理由"}]，最多2条}')
PICK = '用户当前位置附近的景点有：{}。判断照片拍摄于哪一个，只输出JSON：{{"landmark":候选之一或"都不是","confidence":0-1}}'


def pois_with_pic() -> list[dict]:
    found, seen = [], set()

    def walk(x):
        if isinstance(x, dict):
            if x.get("mainPic") and x.get("name") and x["name"] not in seen:
                seen.add(x["name"])
                found.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(json.loads((settings.fixture_dir / "search-poi.json").read_text(encoding="utf-8")))
    return found


def timed(llm: Bailian, url: str, prompt: str) -> tuple[float, dict]:
    t = time.time()
    try:
        out = llm.vision_json(url, prompt)
    except LLMError as e:
        out = {"landmark": f"出错：{e}"}
    return time.time() - t, out


pois = pois_with_pic()
random.seed(1)
sample = random.sample(pois, min(N, len(pois)))
names = [p["name"] for p in pois]
llm = Bailian()
hits = {"盲识别": 0, "候选选择": 0}
for p in sample:
    dt1, blind = timed(llm, p["mainPic"], BLIND)
    cands = random.sample([n for n in names if n != p["name"]], 4) + [p["name"]]
    random.shuffle(cands)
    dt2, pick = timed(llm, p["mainPic"], PICK.format("、".join(cands)))
    hits["盲识别"] += blind.get("landmark") == p["name"]
    hits["候选选择"] += pick.get("landmark") == p["name"]
    print(f"{p['name'][:14]:14} | 盲识别 {str(blind.get('landmark'))[:14]:14} {blind.get('confidence')} {dt1:4.1f}s"
          f" | 候选选择 {str(pick.get('landmark'))[:14]:14} {dt2:4.1f}s", flush=True)
print({k: f"{v}/{len(sample)}" for k, v in hits.items()})
