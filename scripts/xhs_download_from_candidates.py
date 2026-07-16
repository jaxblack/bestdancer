#!/usr/bin/env python3
"""Download Xiaohongshu videos directly from candidates/xiaohongshu.json.

Unlike xiaohongshu_download.py which needs a config, this script reads the
candidates JSON directly, visits each note through logged-in CDP Chrome, and
captures the .mp4 stream URL via response interception.

Writes assets/incoming/<week>/dl2/xiaohongshu_<id>.mp4 + .json (normalized shape)
so rebuild_from_dl2.py can pick them up.
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--max", type=int, default=15)
args = parser.parse_args()

BASE = REPO / "assets" / "incoming" / args.week
CAND = BASE / "candidates" / "xiaohongshu.json"
DL2 = BASE / "dl2"
DL2.mkdir(parents=True, exist_ok=True)

DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),
         (r"hiphop|hip[- ]?hop|嘻哈","Hip-hop"),(r"popping|机械","Popping"),
         (r"locking","Locking"),(r"kpop|k-pop|女团|男团|翻跳|cover","K-pop"),
         (r"choreo|编舞","Choreography"),(r"dance|舞","Dance")]

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t):
            return name
    return "Street"

def note_id(url: str) -> str:
    m = re.search(r"/explore/([0-9a-f]+)", url, re.I)
    return m.group(1) if m else ""

def capture_video_url(page, url: str) -> str:
    target = note_id(url)
    captured: list[str] = []
    def observe(response):
        u = response.url
        if any(k in u for k in [".mp4", "video/tos", "sns-video", "video_mp4",
                                 "sns-video-al", "sns-video-bd", "sns-video-hw"]):
            if u not in captured:
                captured.append(u)
    page.on("response", observe)
    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        # give the SPA time to render, then try clicking play + scrubbing
        time.sleep(2)
        # try clicking anywhere on video area to trigger play
        for sel in ["video", ".xgplayer", ".player-container", ".note-content"]:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=500):
                    el.click(timeout=1500, force=True)
                    break
            except Exception:
                continue
        # sometimes need to unmute / play via JS
        try:
            page.evaluate("""
              () => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; v.play().catch(()=>{}); }
              }
            """)
        except Exception:
            pass
        # wait up to 15s for network capture
        for _ in range(30):
            time.sleep(0.5)
            if captured:
                break
        # fallback: extract video.src / currentSrc directly
        if not captured:
            try:
                src = page.evaluate("""
                  () => {
                    const v = document.querySelector('video');
                    return v ? (v.currentSrc || v.src || '') : '';
                  }
                """)
                if src and src.startswith("http"):
                    captured.append(src)
            except Exception:
                pass
    finally:
        page.remove_listener("response", observe)
    if not captured:
        raise RuntimeError("no video stream captured")
    # prefer the largest / .mp4 one
    def score(u):
        s = 0
        if ".mp4" in u: s += 10
        if "sns-video" in u: s += 5
        return s
    captured.sort(key=score, reverse=True)
    return captured[0]

def probe_duration(mp4: Path) -> int:
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                           capture_output=True, text=True, timeout=15)
        return int(float(r.stdout.strip() or 0))
    except Exception:
        return 0

candidates = json.loads(CAND.read_text(encoding="utf-8"))
print(f"Loaded {len(candidates)} xhs candidates; downloading up to {args.max}", flush=True)

downloaded = 0
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    page = ctx.new_page()
    for i, c in enumerate(candidates):
        if downloaded >= args.max:
            break
        vid = note_id(c.get("url", ""))
        if not vid:
            continue
        mp4_path = DL2 / f"xiaohongshu_{vid}.mp4"
        json_path = DL2 / f"xiaohongshu_{vid}.json"
        if mp4_path.exists() and json_path.exists():
            print(f"[{i+1}] {vid} already have, skip", flush=True)
            downloaded += 1
            continue
        title = c.get("title", "")
        creator = (c.get("creator") or "").lstrip("@") or "unknown"
        like = int(c.get("like", 0))
        print(f"[{i+1}/{len(candidates)}] {vid} | ❤{like} {creator} | {title[:40]}", flush=True)
        try:
            media_url = capture_video_url(page, c["url"])
            resp = ctx.request.get(media_url, timeout=90000)
            if not resp.ok:
                print(f"  fetch failed HTTP {resp.status}", flush=True)
                continue
            body = resp.body()
            if len(body) < 100_000:
                print(f"  too small {len(body)}B", flush=True)
                continue
            mp4_path.write_bytes(body)
            dur = probe_duration(mp4_path)
            meta = {
                "id": vid, "platform": "xiaohongshu", "source": "小红书",
                "desc": title[:500], "author": creator,
                "duration_sec": dur, "like": like,
                "play_count": 0, "tags": [],
                "play_url": c["url"], "url": c["url"],
                "dance_type": infer_dance(title),
            }
            json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            print(f"  OK {mp4_path.stat().st_size//1024}KB [{meta['dance_type']}] {dur}s", flush=True)
            downloaded += 1
        except Exception as e:
            print(f"  ERR {e.__class__.__name__}: {e}"[:200], flush=True)
    page.close()

print(f"=== done: {downloaded} downloaded ===")
