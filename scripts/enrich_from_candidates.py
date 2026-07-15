#!/usr/bin/env python3
"""Re-backfill: enrich existing stub json using candidates/<platform>.json data."""
import json
import re
import subprocess
import sys
from pathlib import Path

WEEK = sys.argv[1] if len(sys.argv) > 1 else "2026-W29"
BASE = Path(f"/Users/jax/bestdancer/assets/incoming/{WEEK}")
DL2 = BASE / "dl2"
CANDS = BASE / "candidates"

DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),
         (r"hiphop|hip[- ]?hop|嘻哈","Hip-hop"),(r"popping|机械","Popping"),
         (r"locking","Locking"),(r"kpop|k-pop|女团|男团|翻跳|cover","K-pop"),
         (r"choreo|编舞","Choreography"),(r"dance|舞", "Dance")]

def infer_dance(text):
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t): return name
    return "Street"

def extract_id(url, platform):
    if platform == "tiktok":
        m = re.search(r"/video/(\d+)", url)
    elif platform == "youtube":
        m = re.search(r"[?&]v=([\w-]+)", url) or re.search(r"/shorts/([\w-]+)", url)
    elif platform == "instagram":
        m = re.search(r"/reel/([\w-]+)", url) or re.search(r"/p/([\w-]+)", url)
    elif platform == "xiaohongshu":
        m = re.search(r"/explore/([\w-]+)", url)
    else:
        m = None
    return m.group(1) if m else ""

for platform in ["tiktok", "youtube", "instagram", "xiaohongshu"]:
    cand_file = CANDS / f"{platform}.json"
    if not cand_file.exists():
        continue
    try:
        cands = json.loads(cand_file.read_text(encoding="utf-8"))
    except Exception:
        continue
    by_id = {}
    for c in cands:
        vid = extract_id(c.get("url", ""), platform)
        if vid:
            by_id[vid] = c
    for mp4 in DL2.glob(f"{platform}_*.mp4"):
        vid = mp4.stem[len(platform)+1:]
        jp = DL2 / f"{mp4.stem}.json"
        try:
            existing = json.loads(jp.read_text(encoding="utf-8")) if jp.exists() else {}
        except Exception:
            existing = {}
        c = by_id.get(vid)
        if not c:
            continue
        # enrich with candidate data
        title = c.get("title", "")
        desc = c.get("source_desc", "") or title
        creator = (c.get("creator") or "").lstrip("@") or existing.get("author", "unknown")
        dance = infer_dance(f"{title} {desc}")
        existing.update({
            "id": vid, "platform": platform,
            "source": {"tiktok":"TikTok","youtube":"YouTube","instagram":"Instagram",
                       "xiaohongshu":"小红书"}[platform],
            "desc": desc[:500],
            "author": creator,
            "like": int(c.get("like", 0)) or int(existing.get("like", 0)),
            "url": c.get("url", existing.get("url","")),
            "play_url": c.get("url", existing.get("play_url","")),
            "dance_type": dance,
            "tags": existing.get("tags", []),
        })
        # ensure duration exists (probe file)
        if not existing.get("duration_sec"):
            try:
                fr = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                                     "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                                    capture_output=True, text=True, timeout=15)
                existing["duration_sec"] = int(float(fr.stdout.strip() or 0))
            except Exception:
                existing["duration_sec"] = 0
        jp.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{platform}] {vid} enriched | {creator} [{dance}] {existing['duration_sec']}s ❤{existing['like']}")

print("done")
