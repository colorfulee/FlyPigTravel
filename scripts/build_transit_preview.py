"""把演示需求用真实服务跑一遍，生成可离线打开的 TravelMate 体验预览页（真实前端 + 录制结果）。

用法：python scripts/build_transit_preview.py [输出目录] [--reuse]      默认 preview_transit/
      --reuse：只改了页面时，直接用已录制的 cases/*.json，不再重跑服务
产物：<输出目录>/cases/case*.json（每个需求的真实返回）和 <输出目录>/preview.html

- 演示需求直接取页面里的 EXAMPLES，页面与录制保持一致
- 逐个串行请求、飞猪结果走缓存，避免触发 flyai 风控
- 预览页按阶段回放（先结论、后行程），和线上体验一致
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.main import _collect, _transit_stages  # noqa: E402

args = [a for a in sys.argv[1:] if not a.startswith("--")]
reuse = "--reuse" in sys.argv
out = Path(args[0] if args else ROOT / "preview_transit")
(out / "cases").mkdir(parents=True, exist_ok=True)
page = (ROOT / "app/static/transit.html").read_text(encoding="utf-8")
examples = json.loads(re.sub(r",\s*\]$", "]", re.search(r"const EXAMPLES = (\[.*?\]);", page, re.S).group(1)))

cases = []
for i, text in enumerate(examples):
    t = time.time()
    saved = out / "cases" / f"case{i}.json"
    if reuse and saved.exists() and json.loads(saved.read_text(encoding="utf-8"))["text"] == text:
        r = json.loads(saved.read_text(encoding="utf-8"))["result"]
    else:
        r = json.loads(_collect(_transit_stages(text)).model_dump_json())
    (out / "cases" / f"case{i}.json").write_text(json.dumps({"text": text, "result": r}, ensure_ascii=False),
                                                 encoding="utf-8")
    v = r["itinerary"]["verdict"] or r["assessment"]["verdict"] or {}
    route = " → ".join(c["name"] for c in r["itinerary"]["cities"])
    print(f"{i} {time.time() - t:5.1f}s | {v.get('policy')} ok={not any(x['level'] == 'block' for x in v.get('issues', []))}"
          f" | {route} | 替代 {len(r['alternatives']['items'])} | 提醒 {len(r['reminders'])}")
    cases.append({"text": text, "result": r})

html = page
# 发布到 Artifact 时会自动包文档骨架：去掉自带的 doctype/html/head/body
html = re.sub(r"<!doctype html>\s*<html[^>]*>\s*<head>\s*<meta charset=\"utf-8\">\s*<meta name=\"viewport\"[^>]*>\s*", "", html)
html = html.replace("</head>\n<body>\n", "").replace("</body>\n</html>\n", "")
html = html.replace("<title>TravelMate · China Visa-Free Planner</title>",
                    '<title>TravelMate</title>\n<meta name="claude:viewport-fit-sides" content="cover">')
# 预览页所在的沙箱不允许页面自己发起下载：日历按钮改为说明，实际服务里照常下载 .ics
ics_click = '  $("ics").onclick = () => {\n'
assert ics_click in html, "页面结构变了：找不到日历按钮"
html = html.replace(ics_click, ics_click + '    if (window.__PREVIEW__) { $("ics").textContent = "📅 Calendar file is available in the full app"; return; }\n', 1)
html = html.replace("  * { box-sizing: border-box; }",
                    "  :root { color-scheme: light; }\n  * { box-sizing: border-box; }\n"
                    "  main { padding-left: max(16px, env(safe-area-inset-left, 0px)) !important; "
                    "padding-right: max(16px, env(safe-area-inset-right, 0px)) !important; }\n"
                    "  .demo { background: var(--info-soft); border: 1px solid #b2ddff; border-radius: 12px; padding: 10px 12px; "
                    "font-size: 13px; margin-top: 12px; }")
html = html.replace("  <section class=\"card\">\n    <label for=\"q\"",
                    f'  <div class="demo"><b>Demo</b>: tap one of the four trips below. Every result was produced on '
                    f'{dt.date.today():%b %d, %Y} by the real service: Alibaba Cloud Bailian (qwen-plus) for understanding, '
                    'live Fliggy trains and flights, Amap for locations, and a rule engine built from China\'s official '
                    'visa-free notices. Nothing was edited by hand. This demo can\'t check new trips; run the service '
                    'locally for that.</div>\n'
                    "  <section class=\"card\">\n    <label for=\"q\"", 1)

shim = """<script>
window.__PREVIEW__ = true;
// 体验预览：用录制好的真实结果回应页面的请求，并按阶段回放（先结论，后行程与替代推荐）
const DEMO = %s;
let lastDemo = null;
const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));
const ndjson = (events, gaps) => new Response(new ReadableStream({
  async start(c) { const enc = new TextEncoder();
    for (let i = 0; i < events.length; i++) { await wait(gaps[i] || 0); c.enqueue(enc.encode(JSON.stringify(events[i]) + "\\n")); }
    c.close(); } }), { headers: { "Content-Type": "application/x-ndjson" } });
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
window.fetch = async (url, opts = {}) => {
  if (url === "/api/transit/snapshot") return lastDemo ? json(200, lastDemo) : json(404, { detail: "No saved result yet" });
  if (url === "/api/transit/stream") {
    const text = JSON.parse(opts.body || "{}").text;
    const hit = DEMO.find((c) => c.text === text);
    if (!hit) return ndjson([{ stage: "error", detail: "This demo only includes the four trips above. Tap one of them, or run the service locally to check your own trip." }], [600]);
    const r = hit.result; lastDemo = r;
    return ndjson([
      { stage: "assessment", assessment: r.assessment },
      { stage: "itinerary", itinerary: r.itinerary, reminders: r.reminders, ics: r.ics },
      { stage: "alternatives", alternatives: r.alternatives },
      { stage: "done" },
    ], [1800, 1500, 900, 0]);
  }
  return json(404, {});
};
</script>
""" % json.dumps(cases, ensure_ascii=False)
html = html.replace("<script>\nconst $ =", shim + "<script>\nconst $ =", 1)
# 预览里点示例直接开始，并默认展示第一个
html = html.replace('''  $("q").value = EXAMPLES[+c.dataset.i];
};''', '''  $("q").value = EXAMPLES[+c.dataset.i]; $("go").click();
};
window.addEventListener("load", () => $("examples").firstElementChild.click());''')
assert "const DEMO =" in html and "$(\"go\").click()" in html, "页面结构变了，预览注入失败"
(out / "preview.html").write_text(html, encoding="utf-8")
print("预览页：", out / "preview.html", f"{len(html) // 1024} KB")
