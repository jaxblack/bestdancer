#!/usr/bin/env python3
"""Download ONLY the videos in cfg['picks'] for a given week.
Writes assets/incoming/<week>/dl2/<platform>_<id>.{mp4,json} so
rebuild_from_dl2.py picks them up.

Uses yt-dlp with cookies.txt (Netscape format) if present.
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--week", required=True)
args = ap.parse_args()

REPO = Path(__file__).resolve().parents[1]
CFG = REPO / "config" / "weekly" / f"{args.week}.json"
BASE = REPO / "assets" / "incoming" / args.week
DL2 = BASE / "dl2"; DL2.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"

cfg = json.loads(CFG.read_text(encoding="utf-8"))
by_id = {c["id"]: c for c in cfg.get("this_week_candidates", []) + cfg.get("classics_pool", [])}
picks = cfg.get("picks", [])

def platform_of(url: str) -> str:
    if "tiktok.com" in url: return "tiktok"
    if "douyin.com" in url: return "douyin"
    if "xiaohongshu.com" in url: return "xiaohongshu"
    if "instagram.com" in url: return "instagram"
    if "youtube.com" in url or "youtu.be" in url: return "youtube"
    if "bilibili.com" in url: return "bilibili"
    return "unknown"

def vid_of(url: str, plat: str) -> str:
    m = {
        "tiktok": r"/video/(\d+)",
        "douyin": r"/video/(\d+)",
        "xiaohongshu": r"/(?:explore|discovery/item)/([a-f0-9]+)",
        "instagram": r"/(?:reel|p)/([^/?#]+)",
        "youtube": r"(?:v=|youtu\.be/|/shorts/)([\w-]+)",
        "bilibili": r"/video/([A-Za-z0-9]+)",
    }.get(plat)
    if not m: return "unknown"
    mo = re.search(m, url)
    return mo.group(1) if mo else url.rsplit("/", 1)[-1].split("?")[0]

failures = []
for p in picks:
    cand = by_id.get(p["id"])
    if not cand:
        print(f"[skip] pick id {p['id']} not in candidates"); continue
    url = cand.get("url", "")
    plat = platform_of(url)
    vid = vid_of(url, plat)
    stem = f"{plat}_{vid}"
    mp4 = DL2 / f"{stem}.mp4"
    meta_out = DL2 / f"{stem}.json"
    if mp4.exists() and meta_out.exists():
        print(f"[skip] already have {stem}.mp4"); continue

    print(f"\n════ #{p.get('rank')} [{plat}] {cand.get('creator','')} — {url[:80]}")
    # Use info-json to get metadata, then merge author into our meta_out
    # 'download' = watermarked TikTok stream but always includes audio.
    # bv+ba merge is unreliable on TikTok (aac column is a lie).
    cmd = ["yt-dlp",
           "-f", "download/bv*+ba/b[acodec!=none]/best",
           "--merge-output-format", "mp4",
           "-o", str(DL2 / f"{stem}.%(ext)s"),
           "--write-info-json",
           "--no-warnings",
           "--restrict-filenames",
           url]
    if COOKIES.exists():
        cmd.extend(["--cookies", str(COOKIES)])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        print(f"  ✗ yt-dlp failed:\n{r.stderr[-500:]}")
        failures.append((p["id"], plat, url, r.stderr[-300:]))
        continue

    # Merge into our own meta
    info_path = DL2 / f"{stem}.info.json"
    info = {}
    if info_path.exists():
        try: info = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception: pass

    meta = {
        "id": vid, "platform": plat, "url": url,
        "author": cand.get("creator", "").lstrip("@") or info.get("uploader") or info.get("channel") or "",
        "title": info.get("title") or cand.get("title", ""),
        "desc": info.get("description") or cand.get("title", ""),
        "duration_sec": int(info.get("duration") or 0),
        "like": int(info.get("like_count") or cand.get("like") or 0),
        "play": int(info.get("view_count") or cand.get("play") or 0),
        "dance_type": cand.get("dance_type", "街舞"),
    }
    meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    print(f"  ✓ mp4 {mp4.stat().st_size//1024}KB, author=@{meta['author']}, title={meta['title'][:40]}")

print(f"\n═══════ SUMMARY ═══════")
print(f"picks: {len(picks)}  failures: {len(failures)}")
for f in failures: print(f"  ✗ {f[1]}/{f[0]}: {f[2][:60]}")
