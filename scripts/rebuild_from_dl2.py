#!/usr/bin/env python3
"""Rebuild weekly config from dl2/*.json across all platforms.

Ranks all downloaded videos (douyin, tiktok, xiaohongshu, instagram, youtube)
by likes and picks top 5 + 1 classic.
"""
import argparse
import json
import shutil
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--week", default="2026-W29")
args = parser.parse_args()

REPO = Path(__file__).resolve().parents[1]
WEEK = args.week
BASE = REPO / "assets" / "incoming" / WEEK
DL2 = BASE / "dl2"
OUT = REPO / "config" / "weekly" / f"{WEEK}.json"

PLATFORM_URLS = {
    "douyin": lambda vid: f"https://www.douyin.com/video/{vid}",
    "tiktok": lambda vid: f"https://www.tiktok.com/video/{vid}",
    "xiaohongshu": lambda vid: f"https://www.xiaohongshu.com/explore/{vid}",
    "instagram": lambda vid: f"https://www.instagram.com/reel/{vid}/",
    "youtube": lambda vid: f"https://www.youtube.com/watch?v={vid}",
}

# Load all json meta
items = []
for jf in DL2.glob("*.json"):
    try:
        d = json.loads(jf.read_text(encoding="utf-8"))
    except Exception:
        continue
    vid = str(d.get("id", jf.stem))
    # locate the actual video file. Prefer <stem>.mp4, else search for matching id.
    mp4 = DL2 / f"{jf.stem}.mp4"
    if not mp4.exists():
        mp4 = DL2 / f"{vid}.mp4"
    if not mp4.exists() or mp4.stat().st_size < 300_000:
        continue
    d.setdefault("platform", "douyin")
    d["_mp4"] = mp4
    items.append(d)

items.sort(key=lambda x: x.get("like", 0), reverse=True)
# de-duplicate same creator (keep highest-liked per author) to diversify the lineup
seen_authors = set()
deduped = []
for it in items:
    key = (it.get("platform", ""), (it.get("author") or "").strip().lower())
    if key in seen_authors:
        continue
    seen_authors.add(key)
    deduped.append(it)
picks = deduped[:6]
labels = ["c1", "c2", "c3", "c4", "c5", "k1"]

# Clean out old c*/k* files
for old in BASE.glob("c*__*.mp4"):
    old.unlink()
for old in BASE.glob("k*__*.mp4"):
    old.unlink()

candidates, picks_list = [], []
classic_entry, narration = None, []
platforms_used = set()

for i, (lab, d) in enumerate(zip(labels, picks)):
    is_classic = (lab == "k1")
    rank = None if is_classic else i + 1
    platform = d.get("platform", "douyin")
    vid = str(d.get("id"))
    platforms_used.add(platform)
    src = d["_mp4"]
    dst = BASE / f"{lab}__{platform}__{vid}.mp4"
    shutil.copy(src, dst)
    dance_type = d.get("dance_type") or "Street"
    author = d.get("author") or "unknown"
    creator_at = "@" + author if author else "@unknown"
    url = d.get("url") or PLATFORM_URLS.get(platform, lambda v: v)(vid)
    print(f"{lab}: [{platform}] {author} [{dance_type}] {d.get('duration_sec', 0)}s ❤{d.get('like', 0)}")
    cand = {
        "id": lab,
        "source": {"douyin": "抖音", "tiktok": "TikTok", "xiaohongshu": "小红书",
                   "instagram": "Instagram", "youtube": "YouTube"}.get(platform, platform),
        "platform": platform,
        "creator": creator_at,
        "title": dance_type,
        "song": "",
        "duration_sec": d.get("duration_sec", 0),
        "like": d.get("like", 0),
        "tags": d.get("tags", []),
        "url": url,
        "dance_type": dance_type,
        "local_path": f"assets/incoming/{WEEK}/{lab}__{platform}__{vid}.mp4",
    }
    difficulty = {"scores": {"tempo": 3, "complexity": 3, "control": 3, "memory": 3, "stamina": 3},
                  "weighted": 3.0, "stars": 3.0, "fit": dance_type, "hardest_part": ""}
    if is_classic:
        classic_entry = {"id": lab, "reason": "同期高热完整编舞", "difficulty": difficulty}
    else:
        picks_list.append({"rank": rank, "id": lab, "reason": "", "highlight_hint": "",
                           "cut_suggestion": "", "difficulty": difficulty})
    narration.append({
        "segment": "classic" if is_classic else "top",
        "rank": rank,
        "vo": "",
        "subtitle": [],
        "on_screen": {"stars": 3.0, "tag": "特别加映" if is_classic else f"本周No.{rank}",
                      "core_moves": [dance_type]},
        "beginner_tip": "",
    })
    candidates.append(cand)

this_week = [c for c in candidates if not c["id"].startswith("k")]
classics = [c for c in candidates if c["id"].startswith("k")]

cfg = {
    "_readme": "auto: cross-platform complete adult choreography, real authors",
    "episode": {"week": WEEK, "theme": "本周编舞精选",
                "platforms": sorted(platforms_used), "voice": "young_female",
                "top_n": 5, "classic_n": 1},
    "this_week_candidates": this_week,
    "classics_pool": classics,
    "picks": picks_list,
    "classic_comeback": classic_entry,
    "narration": narration,
    "metadata": {"source": "cross-platform dance urban/hiphop filtered"},
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print(f"\nWrote {OUT}")
print(f"Platforms used: {sorted(platforms_used)}")
