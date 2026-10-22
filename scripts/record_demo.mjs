// 按 docs/travelmate/演示剧本.md 自动录制演示视频（手机竖屏），用离线预览页回放真实录制结果，每次录出来一样。
//
// 用法：
//   python scripts/build_transit_preview.py --reuse      # 先生成 preview_transit/preview.html
//   node scripts/record_demo.mjs [输出目录]               # 默认 demo_video/
// 产物：demo.mp4（780×1688，30fps）和 chapters.txt（各场景起始时间，配旁白用）
// 需要 Node 版 Playwright（npm i -D playwright，或用 PLAYWRIGHT 指向已安装的 index.js）和 ffmpeg。
//
// 注意：画面里的等待时间是压缩过的回放（先结论、后行程的节奏与线上一致），旁白不要说成现场实时请求。

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const pw = await import(process.env.PLAYWRIGHT || "playwright");
const { chromium } = pw.default || pw;
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OUT = path.resolve(process.argv[2] || path.join(ROOT, "demo_video"));
fs.mkdirSync(OUT, { recursive: true });

// 预览页是给 Artifact 发布用的片段（没有 doctype 骨架）：补上骨架，隐藏 Demo 说明条，不自动点第一个示例
let page = fs.readFileSync(path.join(ROOT, "preview_transit/preview.html"), "utf8");
page = page.replace('window.addEventListener("load", () => $("examples").firstElementChild.click());', "");
page = page.replace(/<div class="demo">[\s\S]*?<\/div>\n/, "");
const html = path.join(OUT, "page.html");
fs.writeFileSync(html, `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head><body>${page}</body></html>`);

const W = 390, H = 844;
const browser = await chromium.launch();
// Playwright 自带录屏不认 deviceScaleFactor（实测页面只占画面左上角四分之一），
// 所以改为后台连续截 2 倍图，再按真实时间戳用 ffmpeg 合成 30fps 视频
const ctx = await browser.newContext({ viewport: { width: W, height: H }, deviceScaleFactor: 2 });
const p = await ctx.newPage();
await p.goto("file://" + html);
const FRAMES = path.join(OUT, "frames");
fs.rmSync(FRAMES, { recursive: true, force: true }); fs.mkdirSync(FRAMES);
const shots = [];
let recording = true;
const recorder = (async () => {
  while (recording) {
    const t = Date.now(), file = path.join(FRAMES, `${String(shots.length).padStart(5, "0")}.jpg`);
    await p.screenshot({ path: file, type: "jpeg", quality: 92 });
    shots.push({ t, file });
    const gap = 66 - (Date.now() - t);  // 约 15 帧 / 秒
    if (gap > 0) await new Promise((r) => setTimeout(r, gap));
  }
})();
const t0 = Date.now();
const chapters = [];
const chapter = (name) => { const s = Math.round((Date.now() - t0) / 1000); chapters.push(`${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}  ${name}`); };
const wait = (ms) => p.waitForTimeout(ms);

// 平滑滚动：把元素顶部滚到屏幕上方 offset 处，用 ms 毫秒
async function glide(selector, ms = 1200, offset = 70) {
  await p.evaluate(([sel, ms, offset]) => new Promise((done) => {
    const el = document.querySelector(sel); if (!el) return done();
    const from = window.scrollY, to = Math.max(0, el.getBoundingClientRect().top + window.scrollY - offset), t = performance.now();
    const step = (now) => { const k = Math.min(1, (now - t) / ms), e = k < .5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      window.scrollTo(0, from + (to - from) * e); k < 1 ? requestAnimationFrame(step) : done(); };
    requestAnimationFrame(step);
  }), [selector, ms, offset]);
}
// 匀速往下读一段：px 像素，用 ms 毫秒
async function read(px, ms) {
  await p.evaluate(([px, ms]) => new Promise((done) => {
    const from = window.scrollY, t = performance.now();
    const step = (now) => { const k = Math.min(1, (now - t) / ms); window.scrollTo(0, from + px * k); k < 1 ? requestAnimationFrame(step) : done(); };
    requestAnimationFrame(step);
  }), [px, ms]);
}
async function finished() {
  await p.waitForSelector("#result:not(.hidden)");
  await p.waitForFunction(() => !document.querySelector("#go").disabled);
}
async function pick(i) {
  await glide("main", 900, 0);
  await p.click(`#examples .chip[data-i="${i}"]`);
}

// 每个场景的时长按 docs/travelmate/视频旁白稿.md 的旁白字数定（约 4 字 / 秒 + 2 秒余量）：
// 画面动作做完后停在当前状态，等到这段旁白念完再进下一个场景
const SECONDS = { 开场: 28, 场景1: 62, 场景2: 22, 场景3: 16, 场景4: 18, 场景5: 20, 收尾: 22, 结束: 0 };
let sceneEnd = 0;
async function scene(key, title) {
  if (sceneEnd) { const left = sceneEnd - Date.now(); if (left > 0) await wait(left); }
  chapter(title);
  sceneEnd = Date.now() + SECONDS[key] * 1000;
}

// 开场：页面与输入
await scene("开场", "开场：页面标题与输入框");
await wait(19000);
await p.click("#q");
await p.type("#q", await p.evaluate(() => EXAMPLES[0]), { delay: 22 });

// 场景 1：九寨沟被拦下，给出替代（主打）
await scene("场景1", "场景 1：九寨沟被拦下（结论 → 倒计时 → 检查清单 → 替代 → 行程 → 提醒）");
await p.click("#go");
await finished();
await glide("#verdict", 900); await wait(17500);     // 旁白：介绍游客 → 结论 → “不是模型说的”
await glide("#clockCard", 1000); await wait(6500);   // 旁白：倒计时
await glide("#issuesCard", 1000); await wait(1500);
await glide("#planCard", 1000); await read(500, 7000);             // 旁白：替代推荐
await glide("#plan", 1000); await read(700, 9000);                 // 旁白：行程与车次
await glide("#remindCard", 1000); await read(900, 9500);           // 旁白：主动提醒

// 场景 2：离境日自动提前一天
await scene("场景2", "场景 2：离境日自动提前一天");
await pick(1); await finished();
await glide("#verdict", 900); await wait(4500);
await glide("#clockCard", 900); await wait(5000);
await glide("#planCard", 900); await read(500, 4500);

// 场景 3：回出发国，不算过境
await scene("场景3", "场景 3：回出发国，不算过境");
await pick(2); await finished();
await glide("#verdict", 900); await wait(4500);
await glide("#issuesCard", 900);

// 场景 4：其实不需要过境免签
await scene("场景4", "场景 4：德国游客可 30 天免签");
await pick(3); await finished();
await glide("#verdict", 900); await wait(5000);
await glide("#planCard", 900); await read(600, 5500);

// 场景 5：一张机票截图代替打字（多模态）
await scene("场景5", "场景 5：机票截图识别");
await pick(4);
await p.waitForSelector("#thumbs .thumb"); await wait(1500);
await finished();
await glide("#verdict", 900); await wait(2500);
await glide("#ticketCard", 900); await wait(6500);
await glide("#clockCard", 900);

// 收尾：来源说明
await scene("收尾", "收尾：规则来源与免责声明");
await glide("#foot", 1500, 300);
await scene("结束", "结束");  // 等收尾旁白念完
chapters.pop();

recording = false; await recorder;
await ctx.close(); await browser.close();
// 每帧显示到下一帧的时间点，保持真实节奏
const list = shots.map((s, i) => `file '${s.file}'\nduration ${(((shots[i + 1]?.t ?? s.t + 66) - s.t) / 1000).toFixed(3)}`);
list.push(`file '${shots.at(-1).file}'`);
const listFile = path.join(OUT, "frames.txt");
fs.writeFileSync(listFile, list.join("\n") + "\n");
const mp4 = path.join(OUT, "demo.mp4");
execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listFile, "-vsync", "cfr", "-r", "30",
  "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]);
fs.rmSync(FRAMES, { recursive: true }); fs.rmSync(listFile); fs.rmSync(html);
fs.writeFileSync(path.join(OUT, "chapters.txt"), chapters.join("\n") + "\n");
console.log(chapters.join("\n"));
console.log("视频：", mp4, `${Math.round(fs.statSync(mp4).size / 1e6)} MB`, `${Math.round((Date.now() - t0) / 1000)} 秒`);
