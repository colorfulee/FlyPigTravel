"""验证 .env 中的百炼与飞猪 Key 是否可用：python scripts/check_keys.py"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings  # noqa: E402
from app.llm import Bailian, LLMError  # noqa: E402

ok = True
print(f"百炼地址：{settings.bailian_base_url}")
try:
    b = Bailian()
    print("  文本", settings.text_model, "→", b.chat_json("只输出 JSON", '回复 {"ok": true}'))
    img = "https://img.alicdn.com/imgextra/i3/O1CN01MbYnKk1kkHO3hCJpa_!!6000000004721-2-tps-1024-1024.png"
    print("  视觉", settings.vl_model, "→", b.vision_json(img, '描述图片，输出 {"desc": "..."}'))
except (LLMError, Exception) as e:  # noqa: BLE001
    ok = False
    print("  失败：", e)

print("飞猪 flyai：")
env = {**os.environ, "FLYAI_API_KEY": settings.flyai_api_key}
r = subprocess.run([settings.flyai_bin, "search-hotel", "--dest-name", "上海", "--hotel-stars", "5"],
                   capture_output=True, text=True, env=env, timeout=60)
out = (r.stdout or r.stderr).strip()
print("  ", out[:300])
if r.returncode != 0 or "xx" in out:
    ok = False
    print("  失败或价格脱敏（出现 xx 说明 FLYAI_API_KEY 未生效）")

print("全部通过" if ok else "有失败项，见上")
sys.exit(0 if ok else 1)
