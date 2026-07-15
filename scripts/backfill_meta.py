#!/usr/bin/env python3
"""Backfill missing dl2/<platform>_<id>.json meta files.

For any dl2/<platform>_<id>.mp4 that lacks a matching .json, fetch metadata
via yt-dlp --dump-json (using per-platform cookies if present) and write
the normalized meta shape rebuild_from_dl2.py expects.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

WEEK = sys.argv[1] if len(sys.argv) > 1 else "2026-W29"
BASE = Path(f"/Users/jax/bestdancer/assets/incoming/{WEEK}")
DL2 = BASE / "dl2"

URL_TMPL = {
    "tiktok": "https://www.tiktok.com/video/{}",
    "youtube": "https://www.youtube.com/watch?v={}",
    "instagram": "https://www.instagram.com/reel/{}/",
    "xiaohongshu": "https://www.xiaohongshu.com/explore/{}",
}

DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),
         (r"hiphop|hip[- ]?hop|嘻哈","Hip-hop"),(r"popping|机械","Popping"),
         (r"locking","Locking"),(r"kpop|k-pop|女团|男团|翻跳|cover","K-pop"),
         (r"choreo|编舞","Choreography")]

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t): return name
    return "Street"

for mp4 in sorted(DL2.glob("*.mp4")):
    stem = mp4.stem
    jp = DL2 / f"{stem}.json"
    if jp.exists():
        continue
    if "_" not in stem:
        # legacy douyin without prefix (id.mp4) — skip (rebuild handles those)
        continue
    platform, vid = stem.split("_", 1)
    url_tmpl = URL_TMPL.get(platform)
    if not url_tmpl:
        continue
    url = url_tmpl.format(vid)
    cookies = BASE / f"cookies_{platform}.txt"
    cmd = ["yt-dlp", "--skip-download", "--no-warnings", "--dump-json",
           "--no-playlist", "--socket-timeout", "20", url]
    if cookies.exists():
        cmd += ["--cookies", str(cookies)]
    print(f"[{platform}] {vid} fetching meta ...", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print(f"  timeout, writing stub")
        r = None
    if r and r.returncode == 0 and r.stdout.strip():
        try:
            d = json.loads(r.stdout.splitlines()[-1])
        except Exception:
            d = {}
    else:
        d = {}
        if r:
            err = (r.stderr or "").strip().splitlines()[:1]
            print(f"  yt-dlp rc={r.returncode}: {' '.join(err)}")
    title = d.get("title") or ""
    desc = d.get("description") or ""
    author = d.get("uploader") or d.get("channel") or d.get("uploader_id") or "unknown"
    dur = int(d.get("duration") or 0)
    likes = int(d.get("like_count") or 0)
    tags = d.get("tags") or []
    # if no duration from meta, probe file with ffprobe
    if not dur:
        try:
            fr = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                                 "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                                capture_output=True, text=True, timeout=15)
            dur = int(float(fr.stdout.strip() or 0))
        except Exception:
            pass
    norm = {
        "id": vid, "platform": platform,
        "source": {"tiktok":"TikTok","youtube":"YouTube","instagram":"Instagram",
                   "xiaohongshu":"小红书"}.get(platform, platform),
        "desc": desc[:500],
        "author": author,
        "duration_sec": dur,
        "like": likes,
        "play_count": int(d.get("view_count") or 0),
        "tags": tags,
        "play_url": d.get("webpage_url") or url,
        "url": d.get("webpage_url") or url,
        "dance_type": infer_dance(f"{title} {desc} {' '.join(tags)}"),
    }
    jp.write_text(json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  OK {norm['author']} [{norm['dance_type']}] {dur}s ❤{likes}")

print("done")
