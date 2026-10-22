"""验证 .env 中的百炼、飞猪、高德 Key 是否可用：python scripts/check_keys.py"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.geo import Geocoder  # noqa: E402
from app.llm import Bailian, LLMError  # noqa: E402

ok = True
print(f"百炼地址：{settings.bailian_base_url}")
try:
    b = Bailian()
    print("  文本", settings.text_model, "→", b.chat_json("只输出 JSON", '回复 {"ok": true}'))
    img = "https://dashscope.oss-cn-beijing.aliyuncs.com/images/dog_and_girl.jpeg"  # 百炼文档示例图
    print("  视觉", settings.vl_model, "→", b.vision_json(img, '描述图片，输出 {"desc": "..."}'))
except (LLMError, Exception) as e:  # noqa: BLE001
    ok = False
    print("  失败：", e)

print("飞猪 flyai：")
env = {**os.environ, "FLYAI_API_KEY": settings.flyai_api_key}
r = subprocess.run([settings.flyai_bin, "search-train", "--origin", "上海", "--destination", "杭州"],
                   capture_output=True, text=True, env=env, timeout=60)
out = (r.stdout or r.stderr).strip()
print("  ", out[:300])
if r.returncode != 0 or re.search(r'"price":\s*"[^"]*x', out):
    ok = False
    print("  失败或价格脱敏（出现 xx 说明 FLYAI_API_KEY 未生效）")

print("高德：")
place = Geocoder().locate("九寨沟")
if place is None:
    ok = False
    print("  失败：AMAP_KEY 未配置或不是“Web服务”类型")
else:
    print(f"  九寨沟 → {place.province} {place.city}（{place.source}）")

print("全部通过" if ok else "有失败项，见上")
sys.exit(0 if ok else 1)
