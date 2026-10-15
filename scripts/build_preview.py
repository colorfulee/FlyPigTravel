"""用实时数据跑 5 个演示需求，生成可离线打开的体验预览页（真实前端 + 录制结果）。

用法：python scripts/build_preview.py [输出目录]      默认 preview_out/
产物：<输出目录>/cases/case*.json（每个需求的真实返回）和 <输出目录>/preview.html

- 逐个串行请求，避免触发 flyai 限流与风控
- 配置了 AMAP_KEY 时，结果里带高德静态地图（data URI），预览页直接显示真实地图
- 酒店主图缩成小图内嵌，预览页不依赖外部图片
"""
import base64
import io
import json
import os
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.flyai import FlyaiClient  # noqa: E402
from app.geo import Geocoder  # noqa: E402
from app.llm import Bailian  # noqa: E402
from app.pipeline import plan  # noqa: E402

CASES = [
    "下周六带5岁孩子住上海漕河泾万丽酒店，想吃自助晚餐、游泳",
    "10月31日带孩子去上海住一晚五星酒店，想要泳池和亲子活动，预算2000以内",
    "这周末和女朋友在上海住一晚，想看江景喝下午茶，预算1500",
    "下个月想一个人在上海安静住一晚，看看展，预算1000左右",
    "周末带爸妈在上海住一晚，想逛园林吃本帮菜，预算1200",
]
ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "preview_out")
(out / "cases").mkdir(parents=True, exist_ok=True)


def thumb(url: str) -> str:
    """酒店主图缩成 336px 宽的 JPEG data URI；没装 Pillow 或下载失败则不放图。"""
    try:
        from PIL import Image
        raw = httpx.get(url, timeout=30).content
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((336, 252))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001
        return ""


llm, fly, geo = Bailian(), FlyaiClient(), Geocoder()
print("高德：", "已配置" if geo.enabled else "未配置（显示示意图）")
cases = []
for i, text in enumerate(CASES):
    r = json.loads(plan(llm, fly, geo, text).model_dump_json())
    r["hotel"]["pic"] = thumb(r["hotel"]["pic"]) if r["hotel"]["pic"] else ""
    (out / "cases" / f"case{i}.json").write_text(json.dumps({"text": text, "result": r}, ensure_ascii=False), encoding="utf-8")
    print(i, r["hotel"]["name"], "| 地图" if r.get("map_image") else "| 示意图", "|", "；".join(r["warnings"])[:80])
    cases.append({"text": text, "result": r})

html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
# 发布到 Artifact 时会自动包文档骨架：去掉自带的 doctype/html/head/body
html = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset=\"utf-8\">\s*<meta name=\"viewport\"[^>]*>\s*", "", html)
html = html.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
html = html.replace("<title>住进去 · 酒店入住旅程设计师</title>",
                    '<title>住进去 · 体验预览</title>\n<meta name="claude:viewport-fit-sides" content="cover">')
html = html.replace("  * { box-sizing: border-box; }",
    "  :root { color-scheme: light; }\n  * { box-sizing: border-box; }\n"
    "  main { padding-left: max(16px, env(safe-area-inset-left, 0px)) !important; padding-right: max(16px, env(safe-area-inset-right, 0px)) !important; }\n"
    "  .preview { background: var(--accent-soft); border: 1px solid #ffc9a3; border-radius: 12px; padding: 10px 12px; font-size: 13px; margin-top: 12px; }\n"
    "  .chip.on { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }\n"
    "  .chip:focus-visible, .tab:focus-visible, button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }")
import datetime as dt  # noqa: E402
html = html.replace('<h1>住进去 <small>让一次入住，成为一段完整旅程</small></h1>',
    '<h1>住进去 <small>让一次入住，成为一段完整旅程</small></h1>\n'
    f'  <div class="preview"><b>体验预览</b>：点下面任一需求即可查看结果。每个结果都是 {dt.date.today().isoformat()} 用飞猪 flyai 实时数据 + 阿里云百炼真实跑出来的，未经人工修改。'
    '预览版不能处理新的输入或截图，自定义需求需要本地运行服务。</div>')
html = html.replace('<label class="upload">📷 上传种草截图找同款<input id="shot" type="file" accept="image/*" hidden></label>',
                    '<span class="muted">上传截图找同款（预览版不可用）</span><input id="shot" type="file" hidden>')
html = html.replace('const safeUrl = (u) => /^https?:\\/\\//.test(u || "") ? u : "";',
                    'const safeUrl = (u) => /^(https?:\\/\\/|data:image\\/)/.test(u || "") ? u : "";')
html = html.replace("navigator.clipboard?.writeText(d.to_confirm[i].script); ev.target.textContent = \"已复制\";",
                    "Promise.resolve(navigator.clipboard?.writeText(d.to_confirm[i].script)).then(() => { ev.target.textContent = \"已复制\"; }, () => { ev.target.textContent = \"复制失败，请手动选择文字\"; });")
shim = """<script>
// 体验预览：用录制好的真实结果回应页面的 API 请求
const DEMO = %s;
let lastDemo = null;
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  if (url === "/api/health") return json(200, { bailian: true, flyai_mode: "live", text_model: "qwen-plus", vl_model: "qwen3-vl-plus" });
  if (url === "/api/snapshot") return lastDemo ? json(200, lastDemo) : json(404, { detail: "暂无历史结果" });
  if (url === "/api/screenshot") return json(400, { detail: "预览版不支持截图识别" });
  if (url === "/api/plan") {
    const text = JSON.parse(opts.body || "{}").text;
    const hit = DEMO.find((c) => c.text === text);
    await new Promise((ok) => setTimeout(ok, 1600));
    if (!hit) return json(400, { detail: "预览版只收录了上面 5 个真实需求，请点选其中一个。自定义需求需要本地运行服务。" });
    lastDemo = hit.result;
    return json(200, hit.result);
  }
  return json(404, {});
};
</script>
""" % json.dumps(cases, ensure_ascii=False)
html = html.replace("<script>\nconst $ =", shim + "<script>\nconst $ =", 1)
html = re.sub(r"const EXAMPLES = \[.*?\];", "const EXAMPLES = DEMO.map((c) => c.text);", html, flags=re.S)
html = html.replace(
    '$("examples").onclick = (ev) => { if (ev.target.classList.contains("chip")) $("q").value = ev.target.textContent; };',
    '$("examples").innerHTML = EXAMPLES.map((e) => `<button type="button" class="chip">${esc(e)}</button>`).join("");\n'
    '$("examples").onclick = (ev) => { const c = ev.target.closest(".chip"); if (!c) return;\n'
    '  [...$("examples").children].forEach((x) => x.classList.toggle("on", x === c)); $("q").value = c.textContent; $("go").click(); };\n'
    'window.addEventListener("load", () => $("examples").firstElementChild.click());')
html = html.replace('<div class="tab ${i ? "" : "on"}" data-i="${i}">', '<div class="tab ${i ? "" : "on"}" data-i="${i}" tabindex="0" role="button">')
(out / "preview.html").write_text(html, encoding="utf-8")
print("预览页：", out / "preview.html", f"{len(html) // 1024} KB")
