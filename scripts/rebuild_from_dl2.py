#!/usr/bin/env python3
"""Rebuild config from dl2/*.json (only complete adult 编舞 videos)."""
import json, shutil, subprocess
from pathlib import Path

REPO = Path("/Users/jax/bestdancer")
WEEK = "2026-W28"
BASE = REPO / "assets" / "incoming" / WEEK
DL2 = BASE / "dl2"
OUT = REPO / "config" / "weekly" / f"{WEEK}.json"

# Load all json meta
items = []
for jf in DL2.glob("*.json"):
    d = json.loads(jf.read_text())
    mp4 = DL2 / f"{d['id']}.mp4"
    if mp4.exists() and mp4.stat().st_size > 300_000:
        items.append(d)

# rank by likes desc, take top 6
items.sort(key=lambda x: x["like"], reverse=True)
picks = items[:6]
labels = ["c1","c2","c3","c4","c5","k1"]

# Clean out old c*/k* files
for old in BASE.glob("c*__*.mp4"): old.unlink()
for old in BASE.glob("k*__*.mp4"): old.unlink()

for lab, d in zip(labels, picks):
    src = DL2 / f"{d['id']}.mp4"
    dst = BASE / f"{lab}__douyin__{d['id']}.mp4"
    shutil.copy(src, dst)
    print(f"{lab}: {d['author']} [{d['dance_type']}] {d['duration_sec']}s ❤{d['like']}")

candidates, picks_list = [], []
classic_entry, narration = None, []
for i, (lab, d) in enumerate(zip(labels, picks)):
    is_classic = (lab == "k1")
    rank = None if is_classic else i + 1
    creator_at = "@" + d["author"] if d["author"] else "@抖音"
    cand = {
        "id": lab,
        "source": "抖音",
        "creator": creator_at,
        "title": d["dance_type"],
        "song": "",
        "duration_sec": d["duration_sec"],
        "like": d["like"],
        "tags": d["tags"],
        "url": f"https://www.douyin.com/video/{d['id']}",
        "dance_type": d["dance_type"],
        "local_path": f"assets/incoming/{WEEK}/{lab}__douyin__{d['id']}.mp4",
    }
    if is_classic:
        classic_entry = {
            "id": lab, "reason": "同期高热完整编舞",
            "difficulty": {"scores":{"tempo":3,"complexity":3,"control":3,"memory":3,"stamina":3},
                           "weighted":3.0,"stars":3.0,"fit":"随意练","hardest_part":"跟节奏"},
        }
    else:
        picks_list.append({
            "rank": rank, "id": lab, "reason": "",
            "highlight_hint": "", "cut_suggestion": "",
            "difficulty": {"scores":{"tempo":3,"complexity":3,"control":3,"memory":3,"stamina":3},
                           "weighted":3.0,"stars":3.0,"fit":d["dance_type"],"hardest_part":""},
        })
    narration.append({
        "segment": "classic" if is_classic else "top",
        "rank": rank,
        "vo": "",  # render_demo.py rebuilds from dance_type + creator
        "subtitle": [],
        "on_screen": {"stars":3.0, "tag":"特别加映" if is_classic else f"本周No.{rank}",
                      "core_moves":[d["dance_type"]]},
        "beginner_tip": "",
    })
    candidates.append(cand)

this_week = [c for c in candidates if not c["id"].startswith("k")]
classics = [c for c in candidates if c["id"].startswith("k")]

cfg = {
    "_readme": "auto: real Douyin adult complete choreography only",
    "episode": {"week": WEEK, "theme": "抖音完整编舞精选",
                "platforms":["douyin"], "voice":"young_female",
                "top_n":5, "classic_n":1},
    "this_week_candidates": this_week,
    "classics_pool": classics,
    "picks": picks_list,
    "classic_comeback": classic_entry,
    "narration": narration,
    "metadata": {"source":"douyin urban 编舞 filtered"},
}
OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print(f"\nWrote {OUT}")
