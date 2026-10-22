"""把 docs/travelmate/视频旁白稿.md 合成为配音，混进 demo_video/demo.mp4。

每行旁白用百炼 qwen3-tts-flash 合成一段，按表里的时间码放进音轨；上一句没读完时顺延。
用法：python scripts/make_voiceover.py [--voice Cherry]
输出：demo_video/demo_voiced.mp4（合成的分句音频缓存在 demo_video/voice/）
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs/travelmate/视频旁白稿.md"
VIDEO = ROOT / "demo_video/demo.mp4"
OUT = ROOT / "demo_video/demo_voiced.mp4"
CACHE = ROOT / "demo_video/voice"
API = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
GAP = 0.3  # 顺延时两句之间的停顿（秒）

# 只影响读法，不改稿子
SAY = {"12367": "幺二三六七", "240 小时": "二百四十小时", "30 天": "三十天", "VL": " V L "}


def lines():
    """读出旁白稿里的 (秒, 旁白)。场景标题行的时间作为该场景的结束边界。"""
    rows, scenes = [], []
    for ln in SCRIPT.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"## (\d+):(\d\d) ", ln):
            scenes.append(int(m[1]) * 60 + int(m[2]))
        elif m := re.match(r"\| (\d+):(\d\d) \|[^|]*\| (.+?) \|$", ln):
            rows.append((int(m[1]) * 60 + int(m[2]), m[3]))
    return rows, scenes


def tts(text, voice):
    for k, v in SAY.items():
        text = text.replace(k, v)
    path = CACHE / f"{hashlib.sha1(f'{voice}|{text}'.encode()).hexdigest()[:12]}.wav"
    if not path.exists():
        body = json.dumps({"model": "qwen3-tts-flash", "input": {"text": text, "voice": voice}})
        curl = ["curl", "-sSf", "-m", "60", "--retry", "3", "--retry-all-errors"]  # urllib 实测偶尔卡住不返回
        res = subprocess.run([*curl, API, "-H", f"Authorization: Bearer {os.environ['DASHSCOPE_API_KEY']}",
                              "-H", "Content-Type: application/json", "-d", body],
                             capture_output=True, text=True, check=True)
        url = json.loads(res.stdout)["output"]["audio"]["url"]
        subprocess.run([*curl, "-o", str(path), url], check=True)
    with wave.open(str(path)) as w:  # 流式返回的 wav 头里长度是占位值，按文件大小算时长
        rate = w.getframerate() * w.getnchannels() * w.getsampwidth()
    return path, (path.stat().st_size - 44) / rate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="Cherry")
    voice = ap.parse_args().voice
    CACHE.mkdir(parents=True, exist_ok=True)
    rows, scenes = lines()
    placed, end, late = [], 0.0, []
    for at, text in rows:
        path, dur = tts(text, voice)
        start = max(at, end + GAP) if end else at
        end = start + dur
        nxt = next((s for s in scenes if s > at), None)
        if nxt and end > nxt:
            late.append(f"{at // 60}:{at % 60:02d} 这句读到 {end:.1f}s，超过下一场景 {nxt}s")
        placed.append((start, path))
        print(f"{start:6.1f}s  {dur:4.1f}s  {text[:24]}")

    inputs = sum((["-i", str(p)] for _, p in placed), [])
    delays = "".join(f"[{i + 1}:a]adelay={int(s * 1000)}:all=1[a{i}];" for i, (s, _) in enumerate(placed))
    mix = "".join(f"[a{i}]" for i in range(len(placed))) + f"amix=inputs={len(placed)}:normalize=0[voice]"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(VIDEO), *inputs,
                    "-filter_complex", delays + mix, "-map", "0:v", "-map", "[voice]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(OUT)], check=True)
    print("输出：", OUT)
    if late:
        print("注意，下面几句超出了所在场景（可在 record_demo.mjs 的 SECONDS 里加秒数后重录画面）：", *late, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main()
