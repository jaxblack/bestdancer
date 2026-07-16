#!/usr/bin/env python3
"""TikTok video-only discovery + yt-dlp download via logged-in Chrome CDP.

Simpler than discover_followed: just grab all a[href*='/video/'] from
search page, then hand off to yt-dlp with per-platform cookies.
"""
from __future__ import annotations
import argparse, json, re, subprocess, time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--keywords", default="urban dance choreography|hiphop choreography|kpop dance cover|jazz choreography|street dance")
parser.add_argument("--per-keyword", type=int, default=6)
parser.add_argument("--max-download", type=int, default=10)
args = parser.parse_args()

WEEK = args.week
BASE = REPO / "assets" / "incoming" / WEEK
DL2 = BASE / "dl2"; DL2.mkdir(parents=True, exist_ok=True)
CAND_DIR = BASE / "candidates"; CAND_DIR.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies_tiktok.txt"

DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),(r"hiphop|hip[- ]?hop","Hip-hop"),
         (r"popping","Popping"),(r"locking","Locking"),
         (r"kpop|k-pop|cover","K-pop"),(r"choreo","Choreography"),(r"dance","Dance")]
EXCLUDE = re.compile(r"tutorial|lesson|kids?|儿童|教学", re.I)

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t): return name
    return "Street"

def vid_from_url(url: str) -> str:
    m = re.search(r"/video/(\d+)", url); return m.group(1) if m else ""

def probe_duration(mp4: Path) -> int:
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                           capture_output=True, text=True, timeout=15)
        return int(float(r.stdout.strip() or 0))
    except Exception: return 0

# ── discover ──
keywords = [k.strip() for k in args.keywords.split("|") if k.strip()]
all_cands = []
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    page = ctx.new_page()
    for kw in keywords:
        try:
            page.goto(f"https://www.tiktok.com/search?q={quote(kw)}",
                      timeout=40000, wait_until="domcontentloaded")
            time.sleep(5)
            # scroll a bit
            page.mouse.wheel(0, 1500); time.sleep(1.5)
            cards = page.locator('a[href*="/video/"]').evaluate_all("""anchors => anchors.map(a => {
                const card = a.closest('[class*=DivItemContainer], article, [class*=video-card]') || a.parentElement;
                return { href: a.href, text: (card?.innerText || '').slice(0, 300) };
            }).filter((v,i,arr) => arr.findIndex(x => x.href === v.href) === i)""")
            got = 0
            for c in cards:
                if got >= args.per_keyword: break
                vid = vid_from_url(c["href"])
                if not vid: continue
                creator_m = re.search(r"tiktok\.com/@([^/]+)/", c["href"])
                creator = creator_m.group(1) if creator_m else "unknown"
                lines = [x.strip() for x in c["text"].splitlines() if x.strip()]
                # first 3 non-nav lines
                nav = {"综合","用户","视频","直播","照片","For You","Following"}
                content_lines = [l for l in lines if l not in nav][:3]
                title = content_lines[0] if content_lines else ""
                if EXCLUDE.search(" ".join(content_lines)):
                    continue
                all_cands.append({"id": vid, "url": c["href"], "title": title[:200],
                                   "creator": creator, "keyword": kw,
                                   "source_desc": c["text"][:400]})
                got += 1
            print(f"[discover] {kw!r} -> {got} anchors", flush=True)
        except Exception as e:
            print(f"[discover] {kw!r} failed: {e}", flush=True)
    page.close()

# dedupe
seen = {}
for c in all_cands:
    if c["id"] not in seen: seen[c["id"]] = c
uniq = list(seen.values())
(CAND_DIR / "tiktok.json").write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"=== total unique tiktok candidates: {len(uniq)}; downloading up to {args.max_download} ===", flush=True)

# ── download via yt-dlp ──
downloaded = 0
for i, c in enumerate(uniq):
    if downloaded >= args.max_download: break
    vid = c["id"]
    mp4 = DL2 / f"tiktok_{vid}.mp4"
    jp = DL2 / f"tiktok_{vid}.json"
    if mp4.exists() and jp.exists():
        print(f"[dl {i+1}] {vid} exists", flush=True); downloaded += 1; continue
    out_tmpl = str(DL2 / f"tiktok_{vid}.%(ext)s")
    cmd = ["yt-dlp","--no-warnings","--no-playlist","--write-info-json",
           "-f","mp4/bestvideo+bestaudio/best","--socket-timeout","30",
           "-o", out_tmpl, c["url"]]
    if COOKIES.exists(): cmd += ["--cookies", str(COOKIES)]
    print(f"[dl {i+1}/{len(uniq)}] {vid} {c['creator'][:20]} | {c['title'][:40]}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("  timeout", flush=True); continue
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()[-1:] if r.stderr else []
        print(f"  yt-dlp rc={r.returncode}: {' '.join(err)[:150]}", flush=True); continue
    # find actual mp4
    real = next(DL2.glob(f"tiktok_{vid}.*"), None)
    if real and real.suffix != ".mp4":
        real = real.rename(real.with_suffix(".mp4"))
    info_path = DL2 / f"tiktok_{vid}.info.json"
    meta_ext = {}
    if info_path.exists():
        try: meta_ext = json.loads(info_path.read_text())
        except Exception: pass
        info_path.unlink()
    like = int(meta_ext.get("like_count") or 0)
    view = int(meta_ext.get("view_count") or 0)
    desc = meta_ext.get("description") or c["title"]
    dur = int(meta_ext.get("duration") or 0) or probe_duration(mp4)
    tags = meta_ext.get("tags") or []
    meta = {"id": vid, "platform": "tiktok", "source": "TikTok",
            "desc": desc[:500], "author": (meta_ext.get("uploader") or c["creator"]).lstrip("@"),
            "duration_sec": dur, "like": like, "play_count": view,
            "tags": tags, "play_url": c["url"], "url": c["url"],
            "dance_type": infer_dance(f"{desc} {' '.join(tags)}")}
    jp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  OK {mp4.stat().st_size//1024}KB [{meta['dance_type']}] {dur}s ❤{like}", flush=True)
    downloaded += 1

print(f"=== done: {downloaded} downloaded ===")
